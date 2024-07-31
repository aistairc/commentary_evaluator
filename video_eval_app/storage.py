import hashlib
import os
import uuid

from django.core.files.storage import FileSystemStorage


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

def md5(file):
    md5_hash = hashlib.md5()
    for chunk in file.chunks():
        md5_hash.update(chunk)
    return md5_hash.hexdigest()

def md5_file_name(instance, filename):
    h = instance.md5sum
    media_type = type(instance).__name__.lower()
    basename, ext = os.path.splitext(filename)
    return os.path.join(f'{media_type}_files', h[0:1], h[1:2], h + ext.lower())

def uuid_file_name(kind, instance, filename):
    h = str(uuid.uuid4())
    basename, ext = os.path.splitext(filename)
    return os.path.join(f'{kind}_files', h[0:1], h[1:2], h + ext.lower())
