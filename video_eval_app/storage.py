from django.template.defaultfilters import default
from icecream import ic # XXX: remove later

import hashlib
import os
import uuid
import shutil
from contextlib import contextmanager
from tempfile import NamedTemporaryFile

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.files.base import File
from django.db.models.fields.files import FieldFile
import jsonfield
from django.core.files.storage import default_storage
import botocore
import boto3

from .mturk import make_aws_session, NoSessionError


CONTENT_TYPES = {
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


class StoredFileField(jsonfield.JSONField):
    def to_python(self, value):
        result = super().to_python(value)
        if result is not None:
            return StoredFile(result)
        return result

    def from_db_value(self, value, expression, connection):
        result = super().from_db_value(value, expression, connection)
        if result is not None:
            return StoredFile(result)
        return result

    def get_prep_value(self, value):
        if isinstance(value, StoredFile):
            value = dict(value)
        return super().get_prep_value(value)

    def value_to_string(self, obj):
        value = self.value_from_object(obj)
        if isinstance(value, StoredFile):
            value = dict(value)
        return super().value_to_string(obj)


class StoredFile(dict):
    @classmethod
    def save(cls, file, subdir, session=None, location=None, with_md5=False):
        if not file:
            return None

        temp_dir = default_storage.path('tmp')
        os.makedirs(temp_dir, exist_ok=True)
        with NamedTemporaryFile(delete=False, dir=temp_dir) as temp_file:
            md5_hash = hashlib.md5()
            for chunk in file.chunks():
                temp_file.write(chunk)
                md5_hash.update(chunk)
            h = md5_hash.hexdigest()
        path = os.path.join(subdir, md5_file_name(file.name, h))
        real_path = default_storage.path(path)
        os.makedirs(os.path.dirname(real_path), exist_ok=True)
        shutil.move(temp_file.name, real_path)
        data = {
            "name": file.name,
            "path": path,
        }
        instance = cls(data)
        if with_md5:
            instance["md5"] = h
        return instance

    def delocalize(self, session, location):
        if not (session and location):
            return self

        path = self["path"]
        real_path = default_storage.path(path)
        if not os.path.exists(real_path):
            return self

        _, ext = os.path.splitext(path)
        content_type = CONTENT_TYPES.get(ext)

        bucket, dir_key = location.split('/', 1)
        key = f"{dir_key}/{path}" if dir_key else path

        s3 = session.client('s3')
        try:
            # does it exist already on S3?
            s3.head_object(Bucket=bucket, Key=key)
        except botocore.exceptions.ClientError as e:
            # it does not, so upload
            extra_args = {
                'ACL': 'public-read',
                'ContentType': content_type,
            }
            try:
                s3.upload_file(real_path, bucket, key, ExtraArgs=extra_args)
            except boto3.exceptions.S3UploadFailedError:
                print("S3 upload failed") # TODO: handle better?
                return
        os.unlink(real_path)
        self["bucket"] = bucket
        self["key"] = key

    @contextmanager
    def local(self, session=None):
        if bucket := self.get('bucket'):
            if not session:
                raise NoSessionError()
            s3 = session.client('s3')
            key = self["key"]
            temp_dir = default_storage.path('tmp')
            os.path.makedirs(temp_dir, exist_ok=True)
            with NamedTemporaryFile(dir=temp_dir) as temp_file:
                temp_file.close()
                s3.download_file(bucket, key, temp_file.name)
                yield temp_file.name
        else:
            yield default_storage.path(self["path"])

    @property
    def url(self):
        if bucket := self.get('bucket'):
            key = self["key"]
            return f"https://{bucket}.s3.amazonaws.com/{key}"
        else:
            return default_storage.url(self["path"])

