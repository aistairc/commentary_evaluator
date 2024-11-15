from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User

from .models import *

class WorkerInline(admin.StackedInline):
    model = Worker
    can_delete = False

class UserAdmin(BaseUserAdmin):
    inlines = [WorkerInline]

admin.site.unregister(User)
admin.site.register(User, UserAdmin)

admin.site.register(StoredFile)
admin.site.register(DatasetVideo)
admin.site.register(Dataset)
admin.site.register(Segment)
admin.site.register(Project)
admin.site.register(Task)
admin.site.register(Assignment)
