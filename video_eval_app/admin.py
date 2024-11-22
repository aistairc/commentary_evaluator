from os import read
from icecream import ic # DEBUG: remove

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.contrib.auth.models import User
from django.utils.html import format_html
from django.urls import reverse


from .models import *

class WorkerInline(admin.StackedInline):
    model = Worker
    can_delete = False
    extra = 0
    show_change_link = True
    readonly_fields = ['turk_worker_id']

class StoredFileAdmin(admin.ModelAdmin):
    readonly_fields = ['md5sum', 'created_by', 'path', 'bucket', 'key']

    def has_add_permission(self, request):
        return False

class DatasetInline(admin.TabularInline):
    model = Dataset
    extra = 0
    show_change_link = True

class UserAdmin(BaseUserAdmin):
    inlines = [WorkerInline, DatasetInline]

class DatasetVideoInline(admin.TabularInline):
    model = DatasetVideo
    can_delete = False
    extra = 0
    show_change_link = True
    exclude = ['cuts', 'video', 'audio', 'subtitles']
    can_delete = False
    readonly_fields = ['is_cut']

    def has_add_permission(self, request, obj=None):
        return False

class TaskInline(admin.TabularInline):
    model = Task
    can_delete = False
    extra = 0
    show_change_link = True
    exclude = ['results']
    readonly_fields = ['project', 'segment', 'turk_hit_id', 'collected_at']

    def has_add_permission(self, request, obj):
        return False

class SegmentInline(admin.TabularInline):
    model = Segment
    can_delete = False
    extra = 0
    show_change_link = True
    exclude = ['video', 'subtitles', 'start', 'end']
    readonly_fields = ['start_ts', 'end_ts']

    def has_add_permission(self, request, obj):
        return False

class ProjectAdmin(admin.ModelAdmin):
    exclude = ['is_busy', 'messages']
    inlines = [TaskInline]

    def get_readonly_fields(self, request, obj=None):
        # readonly_fields = super().get_readonly_fields(request, obj)
        readonly_fields = ['dataset', 'created_by', 'turk_hit_group_id', 'is_started']
        if obj and obj.is_started:
            readonly_fields += ('questions', 'turk_settings')
        return readonly_fields

class ProjectInline(admin.TabularInline):
    model = Project
    show_change_link = True
    extra = 0
    can_delete = False
    exclude = ['questions', 'turk_settings', 'messages', 'is_busy']
    readonly_fields = ['created_by', 'turk_hit_group_id', 'is_started']

class AssignmentInline(admin.TabularInline):
    model = Assignment
    can_delete = False
    extra = 0
    show_change_link = True
    exclude = ['result', 'feedback']

    def has_add_permission(self, request, obj=None):
        return False

    def get_readonly_fields(self, request, obj=None):
        # readonly_fields = super().get_readonly_fields(request, obj)
        if isinstance(obj, Worker):
            readonly_fields = ['task', 'project', 'turk_assignment_id', 'is_approved']
        else:
            readonly_fields = ['worker', 'turk_assignment_id', 'is_approved']
        # if obj and obj.is_started:
        #     readonly_fields += ('questions', 'turk_settings')
        return readonly_fields

    def project(self, obj):
        if obj.task and obj.task.project:
            admin_url = reverse('admin:%s_%s_change' % (
                obj.task.project._meta.app_label, 
                obj.task.project._meta.model_name), 
                args=[obj.task.project.pk]
            )
            return format_html('<a href="{}">{}</a>', admin_url, obj.task.project.name)
        else:
            return '-'

class DatasetVideoAdmin(admin.ModelAdmin):
    readonly_fields = ['video', 'audio', 'subtitles', 'dataset', 'cuts', 'is_cut']
    inlines = [SegmentInline]

    def has_add_permission(self, request):
        return False

class TaskAdmin(admin.ModelAdmin):
    readonly_fields = ['project', 'segment', 'turk_hit_id', 'collected_at', 'results']
    inlines = [AssignmentInline]

    def has_add_permission(self, request):
        return False

class AssignmentAdmin(admin.ModelAdmin):
    readonly_fields = ['task', 'worker', 'turk_assignment_id', 'is_approved', 'result', 'feedback']

    def has_add_permission(self, request):
        return False

class WorkerAdmin(admin.ModelAdmin):
    readonly_fields = ['user', 'turk_worker_id']
    inlines = [AssignmentInline]
    # can_delete = False
    #
    # def has_delete_permission(self, request, obj=None):
    #     return False

    def has_add_permission(self, request):
        return False

class SegmentAdmin(admin.ModelAdmin):
    exclude = ['start', 'end']
    readonly_fields = ['dataset_video', 'video', 'subtitles', 'start_ts', 'end_ts']
    inlines = [TaskInline]

    def has_add_permission(self, request):
        return False

class DatasetAdmin(admin.ModelAdmin):
    inlines = [DatasetVideoInline, ProjectInline]



admin.site.unregister(User)
admin.site.register(User, UserAdmin)
admin.site.register(Worker, WorkerAdmin)
admin.site.register(StoredFile, StoredFileAdmin)
admin.site.register(Dataset, DatasetAdmin)
admin.site.register(DatasetVideo, DatasetVideoAdmin)
admin.site.register(Segment, SegmentAdmin)
admin.site.register(Project, ProjectAdmin)
admin.site.register(Task, TaskAdmin)
admin.site.register(Assignment, AssignmentAdmin)

admin.site.unregister(Invitation)
