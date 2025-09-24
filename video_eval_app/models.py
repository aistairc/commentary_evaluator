import uuid
from functools import partial
from contextlib import asynccontextmanager

from django.db import models, transaction
from django.db.models import Count, Q, Case, When, IntegerField
from django.conf import settings
from asgiref.sync import sync_to_async
from django.db.models.signals import post_save
from django.dispatch import receiver
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



class UserProfile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name="profile")
    upload_token = models.UUIDField(default=uuid.uuid4, unique=True)

    def __str__(self):
        return f"{self.user.username}'s profile"

    def renew_upload_token(self):
        """Generate a new upload token"""
        self.upload_token = uuid.uuid4()
        self.save()

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
    async def store(cls, file, subdir, session=None, location=None, created_by=None):
        if not file:
            return None

        name, path, md5sum = store_file(file, subdir, session, location)
        defaults = {"name": name}
        if created_by:
            defaults["created_by"] = created_by
        instance, created = await cls.objects.aget_or_create(path=path, md5sum=md5sum, defaults=defaults)

        # If file existed but had no owner, update it with the current user
        if not created and created_by and instance.created_by_id is None:
            instance.created_by = created_by
            await instance.asave(update_fields=['created_by'])

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

    @property
    def is_s3_file(self):
        """Returns True if file is stored in S3"""
        return bool(self.bucket and self.key)

    def get_reference_count(self):
        """Count how many entities reference this file in a single query"""
        # Count all references in one query using conditional aggregation
        result = self.__class__.objects.filter(pk=self.pk).aggregate(
            dataset_video_count=Count('dataset_video_videos', distinct=True) +
                               Count('dataset_video_audios', distinct=True) +
                               Count('dataset_video_subtitles', distinct=True),
            segment_count=Count('segment_videos', distinct=True) +
                         Count('segment_subtitles', distinct=True)
        )

        return result['dataset_video_count'] + result['segment_count']

    async def aget_reference_count(self):
        """Async version of get_reference_count"""
        result = await self.__class__.objects.filter(pk=self.pk).aaggregate(
            dataset_video_count=Count('dataset_video_videos', distinct=True) +
                               Count('dataset_video_audios', distinct=True) +
                               Count('dataset_video_subtitles', distinct=True),
            segment_count=Count('segment_videos', distinct=True) +
                         Count('segment_subtitles', distinct=True)
        )

        return result['dataset_video_count'] + result['segment_count']

    def can_be_deleted(self):
        """Returns True if this is the last reference to the file"""
        return self.get_reference_count() <= 1

    async def acan_be_deleted(self):
        """Async version of can_be_deleted"""
        return await self.aget_reference_count() <= 1

    def is_owned_by(self, user):
        """Returns True if file was uploaded by the given user"""
        return self.created_by == user

    def get_s3_location(self):
        """Returns S3 location string for error messages"""
        if self.is_s3_file:
            return f"s3://{self.bucket}/{self.key}"
        return None

    def get_owner_name(self):
        """Returns uploader's username for error messages"""
        return self.created_by.username if self.created_by else "Unknown"

    async def delete_local_file(self):
        """Delete the local file if it exists"""
        if not self.is_s3_file:
            import os

            @sync_to_async
            def delete_file():
                full_path = os.path.join(settings.MEDIA_ROOT, self.path)
                if os.path.exists(full_path):
                    os.unlink(full_path)
                    return True
                return False

            return await delete_file()
        return False

    async def delete_s3_file(self, session):
        """Delete the S3 file if it exists"""
        if self.is_s3_file:
            async with session.client('s3') as s3:
                try:
                    await s3.delete_object(Bucket=self.bucket, Key=self.key)
                    return True
                except Exception as x:
                    location = self.get_s3_location()
                    owner = self.get_owner_name()
                    raise RuntimeError(f"Failed to delete S3 file {location} (uploaded by {owner}): {x}")
        return False

    async def try_delete_file(self, session=None):
        """Attempt to delete the physical file, return success status and error message"""
        try:
            if self.is_s3_file and session:
                success = await self.delete_s3_file(session)
            else:
                success = await self.delete_local_file()
            return success, None
        except Exception as x:
            return False, str(x)

class Dataset(models.Model):
    name = models.CharField(max_length=255)
    token = models.UUIDField(default=uuid.uuid4)
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, related_name='datasets')
    # videos = models.ManyToManyField(Video, through='DatasetVideo', related_name='datasets')

    @property
    def is_cut(self):
        return self.dataset_videos.filter(is_cut=False).count() == 0

    def renew_token(self):
        self.token = uuid.uuid4()

    def __str__(self):
        return self.name

    def __repr__(self):
        return f'<Dataset #{self.pk}: {self.name}>'

    async def safe_delete_with_files(self, session=None):
        """
        Safely delete dataset and all associated files.
        Returns (success, error_message, failed_files)
        """
        from django.db import transaction

        try:
            # Delegate deletion to each DatasetVideo
            async for dataset_video in self.dataset_videos.all():
                success, error, failed_files = await dataset_video.safe_delete_with_files(session)
                if not success:
                    return False, error, failed_files

            # All DatasetVideos deleted successfully, now delete this dataset
            @sync_to_async
            @transaction.atomic
            def delete_dataset():
                return self.delete()

            await delete_dataset()

            return True, None, []

        except Exception as x:
            return False, f"Failed to delete dataset: {x}", []

    def has_nonowned_files(self, user):
        """
        Fast check if dataset has files not owned by the user using a single count query.
        Returns True if there are any associated files not owned by the user.
        """
        # Count files in DatasetVideos and their Segments not owned by user
        count = StoredFile.objects.filter(
            Q(dataset_video_videos__dataset=self) |
            Q(dataset_video_audios__dataset=self) |
            Q(dataset_video_subtitles__dataset=self) |
            Q(segment_videos__dataset_video__dataset=self) |
            Q(segment_subtitles__dataset_video__dataset=self)
        ).exclude(created_by=user).count()

        return count > 0

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

    async def safe_delete_with_files(self, session=None):
        """
        Safely delete dataset video and all associated files.
        Returns (success, error_message, failed_files)
        """
        from django.db import transaction

        failed_files = []

        try:
            # Delegate deletion to each Segment
            async for segment in self.segments.all():
                success, error, segment_failed_files = await segment.safe_delete_with_files(session)
                if not success:
                    return False, error, segment_failed_files

            # All segments deleted successfully, now handle own files using async operations
            files_to_check = []
            if self.video_id:
                video_file = await StoredFile.objects.aget(pk=self.video_id)
                files_to_check.append(video_file)
            if self.audio_id:
                audio_file = await StoredFile.objects.aget(pk=self.audio_id)
                files_to_check.append(audio_file)
            if self.subtitles_id:
                subtitles_file = await StoredFile.objects.aget(pk=self.subtitles_id)
                files_to_check.append(subtitles_file)

            # Try to delete files that will become orphaned
            for stored_file in files_to_check:
                if await stored_file.acan_be_deleted():
                    success, error = await stored_file.try_delete_file(session)
                    if not success:
                        failed_files.append({
                            'file': stored_file,
                            'error': error,
                            'location': stored_file.get_s3_location() or stored_file.path,
                            'owner': stored_file.get_owner_name()
                        })
                    else:
                        # Physical file deleted successfully, now delete the database record
                        await stored_file.adelete()

            # If any file deletions failed, abort
            if failed_files:
                error_msg = "Cannot delete dataset video: failed to delete files:\n"
                for failed in failed_files:
                    error_msg += f"- {failed['location']} (uploaded by {failed['owner']}): {failed['error']}\n"
                return False, error_msg, failed_files

            # All file deletions succeeded, now delete this dataset video
            @sync_to_async
            @transaction.atomic
            def delete_dataset_video():
                return self.delete()

            await delete_dataset_video()

            return True, None, []

        except Exception as x:
            return False, f"Failed to delete dataset video: {x}", failed_files

    def has_nonowned_files(self, user):
        """
        Fast check if dataset video has files not owned by the user using a single count query.
        Returns True if there are any associated files not owned by the user.
        """
        # Count files in this DatasetVideo and its Segments not owned by user
        count = StoredFile.objects.filter(
            Q(dataset_video_videos=self) |
            Q(dataset_video_audios=self) |
            Q(dataset_video_subtitles=self) |
            Q(segment_videos__dataset_video=self) |
            Q(segment_subtitles__dataset_video=self)
        ).exclude(created_by=user).count()

        return count > 0

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

    async def safe_delete_with_files(self, session=None):
        """
        Safely delete segment and all associated files.
        Returns (success, error_message, failed_files)
        """
        from django.db import transaction

        failed_files = []

        try:
            # Collect files from this Segment using async operations
            files_to_check = []
            if self.video_id:
                video_file = await StoredFile.objects.aget(pk=self.video_id)
                files_to_check.append(video_file)
            if self.subtitles_id:
                subtitles_file = await StoredFile.objects.aget(pk=self.subtitles_id)
                files_to_check.append(subtitles_file)

            # Try to delete files that will become orphaned
            for stored_file in files_to_check:
                if await stored_file.acan_be_deleted():
                    success, error = await stored_file.try_delete_file(session)
                    if not success:
                        failed_files.append({
                            'file': stored_file,
                            'error': error,
                            'location': stored_file.get_s3_location() or stored_file.path,
                            'owner': stored_file.get_owner_name()
                        })
                    else:
                        # Physical file deleted successfully, now delete the database record
                        await stored_file.adelete()

            # If any file deletions failed, abort
            if failed_files:
                error_msg = "Cannot delete segment: failed to delete files:\n"
                for failed in failed_files:
                    error_msg += f"- {failed['location']} (uploaded by {failed['owner']}): {failed['error']}\n"
                return False, error_msg, failed_files

            # All file deletions succeeded, now delete this segment
            @sync_to_async
            @transaction.atomic
            def delete_segment():
                return self.delete()

            await delete_segment()

            return True, None, []

        except Exception as x:
            return False, f"Failed to delete segment: {x}", failed_files

    def has_nonowned_files(self, user):
        """
        Fast check if segment has files not owned by the user using a single count query.
        Returns True if there are any associated files not owned by the user.
        """
        # Count files in this Segment not owned by user
        count = StoredFile.objects.filter(
            Q(segment_videos=self) |
            Q(segment_subtitles=self)
        ).exclude(created_by=user).count()

        return count > 0

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

    async def safe_delete_project(self):
        """
        Delete project and its tasks/assignments.
        Does NOT delete the underlying segments and files.
        """
        try:
            # Get counts for reporting before deletion
            task_count = await self.tasks.acount()
            assignment_count = await Assignment.objects.filter(task__project=self).acount()
            invitation_count = await self.invitations.acount()

            # Delete any pending invitations for this project
            await self.invitations.adelete()

            # Delete the project (tasks and assignments will cascade)
            await self.adelete()

            return True, f"Deleted project with {task_count} tasks, {assignment_count} assignments, and {invitation_count} invitations"

        except Exception as e:
            return False, f"Failed to delete project {self.name}: {str(e)}"

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


# Signal to automatically create UserProfile when User is created
@receiver(post_save, sender=User)
def create_user_profile(sender, instance, created, **kwargs):
    if created:
        UserProfile.objects.create(user=instance)
