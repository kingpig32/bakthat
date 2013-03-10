import peewee
from datetime import datetime
from bakthat.conf import config, DATABASE
import hashlib
import json

database = peewee.SqliteDatabase(DATABASE)


class JsonField(peewee.CharField):
    """Custom JSON field."""
    def db_value(self, value):
        return json.dumps(value)

    def python_value(self, value):
        try:
            return json.loads(value)
        except:
            return value


class BaseModel(peewee.Model):
    class Meta:
        database = database


class Backups(BaseModel):
    """Backups Model."""
    backend = peewee.CharField(index=True)
    backend_hash = peewee.CharField(index=True, null=True)
    backup_date = peewee.IntegerField(index=True)
    filename = peewee.TextField(index=True)
    is_deleted = peewee.BooleanField()
    last_updated = peewee.IntegerField()
    metadata = JsonField()
    size = peewee.IntegerField()
    stored_filename = peewee.TextField(index=True, unique=True)
    tags = peewee.CharField()

    @classmethod
    def match_filename(cls, filename, destination, **kwargs):
        profile = config.get(kwargs.get("profile", "default"))

        s3_key = hashlib.sha512(profile.get("access_key") +
                                profile.get("s3_bucket")).hexdigest()
        glacier_key = hashlib.sha512(profile.get("access_key") +
                                     profile.get("glacier_vault")).hexdigest()

        try:
            fquery = "{0}*".format(filename)
            query = Backups.select().where(Backups.filename % fquery |
                                           Backups.stored_filename % fquery,
                                           Backups.backend == destination,
                                           Backups.backend_hash << [s3_key, glacier_key])
            query = query.order_by(Backups.backup_date.desc())
            return query.get()
        except Backups.DoesNotExist:
            return

    @classmethod
    def search(cls, query="", destination="", **kwargs):
        if not destination:
            destination = ["s3", "glacier"]
        if isinstance(destination, (str, unicode)):
            destination = [destination]

        profile = config.get(kwargs.get("profile", "default"))

        s3_key = hashlib.sha512(profile.get("access_key") +
                                profile.get("s3_bucket")).hexdigest()
        glacier_key = hashlib.sha512(profile.get("access_key") +
                                     profile.get("glacier_vault")).hexdigest()

        query = "*{0}*".format(query)
        wheres = []
        wheres.append(Backups.filename % query |
                      Backups.stored_filename % query)
        wheres.append(Backups.backend << destination)
        wheres.append(Backups.backend_hash << [s3_key, glacier_key])
        wheres.append(Backups.is_deleted == False)

        older_than = kwargs.get("older_than")
        if older_than:
            wheres.append(Backups.backup_date < older_than)

        backup_date = kwargs.get("backup_date")
        if backup_date:
            wheres.append(Backups.backup_date == backup_date)

        last_updated_gt = kwargs.get("last_updated_gt")
        if last_updated_gt:
            wheres.append(Backups.last_updated >= last_updated_gt)

        tags = kwargs.get("tags", [])
        if tags:
            if isinstance(tags, (str, unicode)):
                tags = tags.split()
            tags_query = ["Backups.tags % '*{0}*'".format(tag) for tag in tags]
            tags_query = eval("({0})".format(" and ".join(tags_query)))
            wheres.append(tags_query)

        return Backups.select().where(*wheres).order_by(Backups.last_updated.desc())

    def set_deleted(self):
        self.is_deleted = True
        self.last_updated = int(datetime.utcnow().strftime("%s"))
        self.save()

    @classmethod
    def upsert(cls, **backup):
        q = Backups.select()
        q = q.where(Backups.stored_filename == backup.get("stored_filename"))
        if q.count():
            Backups.update(**backup).where(Backups.stored_filename == backup.get("stored_filename")).execute()
        else:
            Backups.create(**backup)

    class Meta:
        db_table = 'backups'


class Config(BaseModel):
    """key => value config store."""
    key = peewee.CharField(index=True, unique=True)
    value = JsonField()

    @classmethod
    def get_key(self, key, default=None):
        try:
            return Config.get(Config.key == key).value
        except Config.DoesNotExist:
            return default

    @classmethod
    def set_key(self, key, value):
        q = Config.select().where(Config.key == key)
        if q.count():
            Config.update(value=value).where(Config.key == key).execute()
        else:
            Config.create(key=key, value=value)

    class Meta:
        db_table = 'config'


class Inventory(BaseModel):
    """Filename => archive_id mapping for glacier archives."""
    archive_id = peewee.CharField(index=True, unique=True)
    filename = peewee.CharField(index=True)

    @classmethod
    def get_archive_id(self, filename):
        return Inventory.get(Inventory.filename == filename).archive_id

    class Meta:
        db_table = 'inventory'


class Jobs(BaseModel):
    """filename => job_id mapping for glacier archives."""
    filename = peewee.CharField(index=True)
    job_id = peewee.CharField()

    @classmethod
    def get_job_id(cls, filename):
        """Try to retrieve the job id for a filename.

        :type filename: str
        :param filename: Filename

        :rtype: str
        :return: Job Id for the given filename
        """
        try:
            return Jobs.get(Jobs.filename == filename).job_id
        except Jobs.DoesNotExist:
            return

    @classmethod
    def update_job_id(cls, filename, job_id):
        """Update job_id for the given filename.

        :type filename: str
        :param filename: Filename

        :type job_id: str
        :param job_id: New job_id

        :return: None
        """
        q = Jobs.select().where(Jobs.filename == filename)
        if q.count():
            Jobs.update(job_id=job_id).where(Jobs.filename == filename).execute()
        else:
            Jobs.create(filename=filename, job_id=job_id)

    class Meta:
        db_table = 'jobs'


for table in [Backups, Jobs, Inventory, Config]:
    if not table.table_exists():
        table.create_table()


def switch_from_dt_to_peewee():
    import dumptruck
    import os
    import time
    if os.path.isfile(os.path.expanduser("~/.bakthat.dt")):
        dt = dumptruck.DumpTruck(dbname=os.path.expanduser("~/.bakthat.dt"), vars_table="config")
        for backup in dt.dump("backups"):
            try:
                backup["tags"] = " ".join(backup.get("tags", []))
                Backups.upsert(**backup)
                time.sleep(0.1)
            except Exception, exc:
                print exc
        for ivt in dt.dump("inventory"):
            try:
                Inventory.create(filename=ivt["filename"],
                                 archive_id=ivt["archive_id"])
            except Exception, exc:
                print exc
        os.remove(os.path.expanduser("~/.bakthat.dt"))

switch_from_dt_to_peewee()
