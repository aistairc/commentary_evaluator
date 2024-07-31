from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("datasets", views.datasets, name="datasets"),
    path("datasets/new", views.datasets_new, name="datasets_new"),
    # path("datasets/<int:dataset_id>", views.dataset, name="dataset"),
    path("datasets/<int:dataset_id>/edit", views.dataset_edit, name="dataset_edit"),
    path("datasets/<int:dataset_id>/videos", views.dataset_videos, name="dataset_videos"),
    path("datasets/<int:dataset_id>/videos/new", views.dataset_video, name="dataset_videos_new"),
    path("datasets/<int:dataset_id>/videos/<int:dataset_video_id>", views.dataset_video, name="dataset_video"),
    path("datasets/<int:dataset_id>/projects", views.dataset_projects, name="dataset_projects"),
    path("datasets/<int:dataset_id>/projects/<int:project_id>", views.dataset_project, name="dataset_project"),
    path("datasets/<int:dataset_id>/projects/new", views.dataset_project, name="dataset_project_new"),
    path("projects", views.projects, name="projects"),
    path("projects/<int:project_id>/users", views.project_users, name="project_users"),
    path("datasets/<int:dataset_id>/managers", views.dataset_managers, name="dataset_managers"),
    path("projects/<int:project_id>/results", views.project_results, name="project_results"),
    path("projects/<int:project_id>/eval", views.project_eval, name="project_eval"),
    path("segments/<str:segment_id>", views.segment, name="segment"),
    path("tasks/<int:task_id>/submit", views.task_eval_submit, name="task_eval_submit"),

    path("upload_video/<uuid:token>", views.upload_video_api, name="upload_video_api"),
    # path("turk_question", views.turk_question),
]
