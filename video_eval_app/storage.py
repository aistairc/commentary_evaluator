from django.template.defaultfilters import default
from icecream import ic # XXX: remove later

import hashlib
import os
import uuid
import shutil
from contextlib import asynccontextmanager
from tempfile import NamedTemporaryFile

from django.conf import settings
from django.core.files.storage import FileSystemStorage
from django.core.files.base import File
from django.db.models.fields.files import FieldFile
import jsonfield
from django.core.files.storage import default_storage
import botocore
import boto3

from .mturk import make_aws_session


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

def store_file(file, subdir, session, location):
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
    
    try:
        os.makedirs(os.path.dirname(real_path), exist_ok=True)
        shutil.move(temp_file.name, real_path)
    except Exception as e:
        # Clean up temp file if move fails
        if os.path.exists(temp_file.name):
            os.unlink(temp_file.name)
        raise e
    
    return file.name, path, h

async def delocalize_file(path, session, location):
    if not (session and location):
        return None

    real_path = default_storage.path(path)
    if not os.path.exists(real_path):
        return None

    _, ext = os.path.splitext(path)
    content_type = CONTENT_TYPES.get(ext)

    bucket, *dir_list = location.split('/', 1)
    if dir_list:
        dir_key = dir_list[0]
    else:
        dir_key = ""
    key = f"{dir_key}/{path}" if dir_key else path

    async with session.client('s3') as s3:
        try:
            # does it exist already on S3?
            await s3.head_object(Bucket=bucket, Key=key)
        except botocore.exceptions.ClientError as e:
            # it does not, so upload
            extra_args = {
                'ACL': 'public-read',
                'ContentType': content_type,
            }
            try:
                await s3.upload_file(real_path, bucket, key, ExtraArgs=extra_args)
            except boto3.exceptions.S3UploadFailedError as e:
                logger.error(f"S3 upload failed for {key} to bucket {bucket}: {str(e)}")
                # Clean up local file since upload failed
                os.unlink(real_path)
                raise RuntimeError(f"Failed to upload file to S3: {str(e)}")
            except Exception as e:
                logger.error(f"Unexpected error during S3 upload for {key}: {str(e)}")
                # Clean up local file since upload failed
                os.unlink(real_path)
                raise RuntimeError(f"File upload failed: {str(e)}")
    
    # Delete local file - either upload succeeded or file already existed on S3
    os.unlink(real_path)
    return bucket, key

@asynccontextmanager
async def local_file(path, bucket, key, session):
    if bucket:
        temp_dir = default_storage.path('tmp')
        os.makedirs(temp_dir, exist_ok=True)
        with NamedTemporaryFile(dir=temp_dir) as temp_file:
            temp_file.close()
            async with session.client('s3') as s3:
                await s3.download_file(bucket, key, temp_file.name)
            yield temp_file.name
    else:
        yield default_storage.path(path)
