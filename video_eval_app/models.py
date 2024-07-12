import hashlib
import uuid

from django.db import models
from django.conf import settings
from django.core.files.storage import FileSystemStorage

from django.contrib.auth.models import User, Group


# Thanks to https://stackoverflow.com/a/15900958/240443
class MediaFileSystemStorage(FileSystemStorage):
    def get_available_name(self, name, max_length=None):
        if max_length and len(name) > max_length:
            raise(Exception("name's length is greater than max_length"))
        return name

    def _save(self, name, content):
        if self.exists(name):
            # if the file exists, do not call the superclasses _save method
            return name
        # if the file is new, DO call it
        return super(MediaFileSystemStorage, self)._save(name, content)

media_storage = MediaFileSystemStorage()

def media_file_name(instance, filename):
    h = instance.md5sum
    media_type = type(instance).__name__.lower()
    basename, ext = os.path.splitext(filename)
    return os.path.join(f'{media_type}_files', h[0:1], h[1:2], h + ext.lower())

def md5(file):
    md5_hash = hashlib.md5()
    for chunk in file.chunks():
        md5_hash.update(chunk)
    return md5_hash.hexdigest()

class MediaModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    file = models.FileField(upload_to=media_file_name, storage=media_storage)
    md5sum = models.CharField(max_length=36, primary_key=True)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.md5sum = md5(self.file)

    class Meta:
        abstract = True


class Worker(models.Model):
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True)
    turk_worker_id = models.CharField(max_length=255, blank=True)

class Video(MediaModel):
    pass

class Dataset(models.Model):
    name = models.CharField(max_length=255, blank=True)
    token = models.UUIDField(default=uuid.uuid4)
    owner_user = models.ForeignKey(User, on_delete=models.CASCADE)

    class Meta:
        indexes = [
            models.Index(fields=["token"]),
        ]

class DatasetVideo(models.Model):
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE)
    video = models.ForeignKey(Video, on_delete=models.CASCADE)
    subtitles = models.FileField(upload_to='subs', null=True)
    name = models.CharField(max_length=255, blank=True)
    cuts = models.JSONField(default=list)
    is_cut = models.BooleanField(default=False)

class Segment(MediaModel):
    dataset_video = models.ForeignKey(DatasetVideo, on_delete=models.CASCADE)
    start = models.FloatField()
    end = models.FloatField()
    subtitles = models.FileField(upload_to='segment_subs', null=True)
    is_ready = models.BooleanField(default=False)

class Project(models.Model):
    name = models.CharField(max_length=255, null=True)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE)
    owner_user = models.ForeignKey(User, on_delete=models.CASCADE)
    questions = models.JSONField(default=dict)
    turk_settings = models.JSONField(default=dict)

class Task(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    name = models.CharField(max_length=255, null=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE)
    segment = models.ForeignKey(Segment, on_delete=models.CASCADE)
    turk_hit_id = models.CharField(max_length=255, blank=True)
    # collected results
    collected_at = models.DateTimeField(null=True)
    results = models.JSONField(null=True)

class Assignment(models.Model):
    class Status(models.IntegerChoices):
        PENDING = 0
        SUBMITTED = 1
        REJECTED = 2
        ACCEPTED = 3
    created_at = models.DateTimeField(auto_now_add=True)
    segment_created_at = models.DateTimeField()
    worker = models.ForeignKey(Worker, on_delete=models.CASCADE)
    turk_assignment_id = models.CharField(max_length=255, null=True)
    status = models.IntegerField(choices=Status)
    result = models.JSONField(null=True)
