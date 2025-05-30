import uuid
from functools import partial
from contextlib import asynccontextmanager

from django.db import models
from django.conf import settings
# from django.db.models.signals import post_save
# from django.dispatch import receiver
from django.core.files.storage import default_storage
from django.contrib.auth.models import User, Group
from django_q.tasks import async_task
from invitations.models import Invitation as OriginalInvitation
import jsonfield


from .utils import secs_to_timestamp
from .storage import delocalize_file, store_file, local_file

CONTENT_TYPES = {
    ".avi": "video/x-msvideo",
    ".mkv": "video/x-matroska",
    ".mp4": "video/mp4",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".aac": "audio/aac",
    ".vtt": "text/vtt",
    ".csv": "text/csv",
    ".srt": "text/plain",
}


def md5_sum(file):
    md5_hash = hashlib.md5()
    for chunk in file.chunks():
        md5_hash.update(chunk)
    h = md5_hash.hexdigest()
    return h

def md5_file_name(name, h):
    _, ext = os.path.splitext(name)
    return os.path.join(h[0], h[1], h + ext.lower())



class Worker(models.Model):
    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, related_name="worker")
    worker_id = models.CharField(max_length=255, blank=True)
    service = models.CharField(max_length=255, blank=True)

    def __str__(self):
        if self.user:
            return self.user.username
        if self.worker_id:
            name = self.worker_id
            if self.service:
                name = f"{name}@{self.service}"
            return name

    def __repr__(self):
        return f'<Worker #{self.pk}: {self}>'

class StoredFile(models.Model):
    md5sum = models.CharField(max_length=36, primary_key=True)
    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='stored_files')
    name = models.CharField(max_length=255)
    path = models.CharField(max_length=255)
    bucket = models.CharField(max_length=255, blank=True)
    key = models.CharField(max_length=255, blank=True)

    @classmethod
    async def store(cls, file, subdir, session=None, location=None):
        if not file:
            return None

        name, path, md5sum = store_file(file, subdir, session, location)
        instance, _ = await cls.objects.aget_or_create(path=path, md5sum=md5sum, defaults={ "name": name })
        return instance

    async def delocalize(self, session, location):
        if result := await delocalize_file(self.path, session, location):
            self.bucket, self.key = result
            await self.asave()
        return self

    @asynccontextmanager
    async def local(self, session=None):
        async with local_file(self.path, self.bucket, self.key, session) as name:
            yield name

    @property
    def url(self):
        if self.bucket:
            return f"https://{self.bucket}.s3.amazonaws.com/{self.key}"
        else:
            return default_storage.url(self.path)

    def absolute_url(self, request):
        if self.bucket:
            return self.url
        else:
            return request.build_absolute_uri(self.url)

    def __str__(self):
        return self.path

    def __repr__(self):
        s = f'pk={self.pk}, path={self.path}'
        if self.bucket:
            s += f" bucket={self.bucket}, key={self.key}"
        return f'<StoredFile: {s}>'

class Dataset(models.Model):
    name = models.CharField(max_length=255)
    token = models.UUIDField(default=uuid.uuid4)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='datasets')
    # videos = models.ManyToManyField(Video, through='DatasetVideo', related_name='datasets')

    @property
    def is_cut(self):
        self.dataset_videos.filter(is_cut=False).count() == 0

    def renew_token(self):
        self.token = uuid.uuid4()

    def __str__(self):
        return self.name

    def __repr__(self):
        return f'<Dataset #{self.pk}: {self.name}>'

    class Meta:
        permissions = (
            ('manage_dataset', 'Manage dataset'),
        )
        indexes = [
            models.Index(fields=["token"]),
        ]

class DatasetVideo(models.Model):
    # TODO: rename to Video (also, dataset_videos -> videos)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name='dataset_videos')
    video = models.ForeignKey(StoredFile, on_delete=models.CASCADE, related_name='dataset_video_videos')
    subtitles = models.ForeignKey(StoredFile, on_delete=models.SET_NULL, related_name='dataset_video_subtitles', null=True)
    audio = models.ForeignKey(StoredFile, on_delete=models.SET_NULL, related_name='dataset_video_audios', null=True)
    name = models.CharField(max_length=255)
    cuts = jsonfield.JSONField(default=list, blank=True)
    is_cut = models.BooleanField(default=False)

    def __str__(self):
        return self.name

    def __repr__(self):
        return f'<DatasetVideo #{self.pk}: {self.name}>'

    class Meta:
        unique_together = [['dataset', 'video']]

# @receiver(post_save, sender=DatasetVideo)
# def dataset_video_changed(sender, instance, **kwargs):
#     async_task('video_eval_app.tasks.cut_dataset_video', instance)


class Segment(models.Model):
    video = models.ForeignKey(StoredFile, on_delete=models.CASCADE, related_name='segment_videos')
    subtitles = models.ForeignKey(StoredFile, on_delete=models.SET_NULL, related_name='segment_subtitles', null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    dataset_video = models.ForeignKey(DatasetVideo, on_delete=models.CASCADE, related_name='segments')
    start = models.FloatField()
    end = models.FloatField(null=True)

    @property
    def start_ts(self):
        return secs_to_timestamp(self.start)

    @property
    def end_ts(self):
        return secs_to_timestamp(self.end)

    def __repr__(self):
        return f'<Segment #{self.pk}>'

    def __str__(self):
        return f'{self.dataset_video.name} ({self.start_ts} - {self.end_ts})'

class Project(models.Model):
    class WorkerIdentity(models.IntegerChoices):
        ANONYMOUS = 0, 'Anonymous'
        NUMBERED = 1, 'Numbered'
        HASHED = 2, 'Hashed'
        USERNAME = 3, 'Username'

    name = models.CharField(max_length=255)
    worker_identity = models.IntegerField(choices=WorkerIdentity.choices, default=WorkerIdentity.ANONYMOUS)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name='projects')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='projects')
    questions = jsonfield.JSONField(default=list)
    turk_settings = jsonfield.JSONField(null=True)
    turk_hit_group_id = models.CharField(max_length=255, blank=True)
    is_started = models.BooleanField(default=False)
    is_busy = models.BooleanField(default=False)
    messages = jsonfield.JSONField(default=list)

    def __repr__(self):
        return f'<Project #{self.pk}: {self.name}>'

    def __str__(self):
        return self.name

    class Meta:
        permissions = (
            ('manage_project', 'Manage project'),
            ('evaluate_project', 'Evaluate project'),
        )

class Task(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name='tasks')
    segment = models.ForeignKey(Segment, on_delete=models.CASCADE, related_name='segments')
    turk_hit_id = models.CharField(max_length=255, blank=True)
    # collected results
    collected_at = models.DateTimeField(null=True)
    results = jsonfield.JSONField(null=True)

    def __repr__(self):
        return f'<Task #{self.pk}>'

    def __str__(self):
        return str(self.segment)

class Assignment(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='assignments')
    worker = models.ForeignKey(Worker, on_delete=models.CASCADE, related_name='assignments')
    turk_assignment_id = models.CharField(max_length=255, null=True)
    is_approved = models.BooleanField(null=True)
    result = jsonfield.JSONField(null=True)
    feedback = models.CharField(max_length=255)

    def __repr__(self):
        return f'<Assignment #{self.pk}>'

    def __str__(self):
        return f'{self.task} by {self.worker}'



class Invitation(OriginalInvitation):
    dataset = models.ForeignKey(Dataset, on_delete=models.SET_NULL, related_name='invitations', null=True)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, related_name='invitations', null=True)
    role = models.CharField(max_length=255)
