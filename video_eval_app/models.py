import uuid
from functools import partial

from django.db import models
from django.conf import settings
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.contrib.auth.models import User, Group
from django_q.tasks import async_task
from invitations.models import Invitation as OriginalInvitation
import jsonfield


from .utils import secs_to_timestamp
from .storage import md5 as _md5, md5_file_name as _md5_file_name, uuid_file_name as _uuid_file_name, media_storage as _media_storage


class MediaModel(models.Model):
    created_at = models.DateTimeField(auto_now_add=True)
    file = models.FileField(upload_to=_md5_file_name, storage=_media_storage)
    md5sum = models.CharField(max_length=36, primary_key=True)

    @classmethod
    def get_or_create(klass, file, *args, **kwargs):
        md5sum = _md5(file)
        try:
            instance = klass.objects.get(md5sum=md5sum)
        except klass.DoesNotExist:
            instance = klass.objects.create(md5sum=md5sum, file=file, *args, **kwargs)
        return instance

    class Meta:
        abstract = True



class Worker(models.Model):
    user = models.OneToOneField(User, on_delete=models.SET_NULL, null=True, related_name="worker")
    turk_worker_id = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f'pk={self.pk}, user={self.user_id}, turk_worker_pk={self.turk_worker_id}'

class Video(MediaModel):
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='videos')

    def __str__(self):
        return f'pk={self.pk}'

class Dataset(models.Model):
    name = models.CharField(max_length=255)
    token = models.UUIDField(default=uuid.uuid4)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='datasets')
    videos = models.ManyToManyField(Video, through='DatasetVideo', related_name='datasets')

    @property
    def is_cut(self):
        self.dataset_videos.filter(is_cut=False).count() == 0

    def renew_token(self):
        self.token = uuid.uuid4()

    def __str__(self):
        return f'pk={self.pk}, name={self.name}'

    class Meta:
        permissions = (
            ('manage_dataset', 'Manage dataset'),
        )
        indexes = [
            models.Index(fields=["token"]),
        ]

_sub_file_name = partial(_uuid_file_name, 'sub')
_audio_file_name = partial(_uuid_file_name, 'audio')
class DatasetVideo(models.Model):
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name='dataset_videos')
    video = models.ForeignKey(Video, on_delete=models.CASCADE, related_name='dataset_videos')
    subtitles = models.FileField(upload_to=_sub_file_name, null=True)
    audio = models.FileField(upload_to=_audio_file_name, null=True)
    name = models.CharField(max_length=255)
    cuts = jsonfield.JSONField(default=list, null=True)
    is_cut = models.BooleanField(default=False)

    def __str__(self):
        return f'pk={self.pk}, dataset={self.dataset_id}, name={self.name}, video={self.video_id}, audio={self.audio}, subtitles={self.subtitles}, cuts={self.cuts}, is_cut={self.is_cut}'

    class Meta:
        unique_together = [['dataset', 'video']]

@receiver(post_save, sender=DatasetVideo)
def dataset_video_changed(sender, instance, **kwargs):
    async_task('video_eval_app.tasks.cut_dataset_video', instance)


_segment_sub_file_name = partial(_uuid_file_name, 'segment_sub')
class Segment(MediaModel):
    dataset_video = models.ForeignKey(DatasetVideo, on_delete=models.CASCADE, related_name='segments')
    start = models.FloatField()
    end = models.FloatField(null=True)
    subtitles = models.FileField(upload_to=_segment_sub_file_name, null=True)

    @property
    def start_ts(self):
        return secs_to_timestamp(self.start)

    @property
    def end_ts(self):
        return secs_to_timestamp(self.end)

    # def __str__(self):
    #     return f'pk={self.pk}, dataset_video={self.dataset_video_id}, start={self.start}, end={self.end}, subtitles={self.subtitles}'

class Project(models.Model):
    name = models.CharField(max_length=255)
    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE, related_name='projects')
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='projects')
    questions = jsonfield.JSONField(default=list)
    turk_settings = jsonfield.JSONField(default=dict)

    def __str__(self):
        return f'pk={self.pk}, dataset={self.dataset_id}'

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

    def __str__(self):
        return f'pk={self.pk}, project={self.project_id}, segment={self.segment_id}, turk_hit_id={self.turk_hit_id}, results={self.results}'

class Assignment(models.Model):
    class Status(models.IntegerChoices):
        PENDING = 0
        SUBMITTED = 1
        REJECTED = 2
        ACCEPTED = 3
        LOCAL = 4
    created_at = models.DateTimeField(auto_now_add=True)
    task = models.ForeignKey(Task, on_delete=models.CASCADE, related_name='assignments')
    segment_created_at = models.DateTimeField()
    worker = models.ForeignKey(Worker, on_delete=models.CASCADE, related_name='assignments')
    turk_assignment_id = models.CharField(max_length=255, null=True)
    status = models.IntegerField(choices=Status)
    result = jsonfield.JSONField(null=True)

    def __str__(self):
        return f'pk={self.pk}, task={self.task_id}, worker={self.worker_id}, turk_assignment_id={self.turk_assignment_id}, status={self.status}'



class Invitation(OriginalInvitation):
    dataset = models.ForeignKey(Dataset, on_delete=models.SET_NULL, related_name='invitations', null=True)
    project = models.ForeignKey(Project, on_delete=models.SET_NULL, related_name='invitations', null=True)
    role = models.CharField(max_length=255)
