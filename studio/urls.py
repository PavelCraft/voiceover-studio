from django.urls import path
from . import views

urlpatterns = [
    path("", views.home, name="home"),
    path("start/", views.upload_audio, name="upload-audio"),
    path("projects/", views.dashboard, name="dashboard"),
    path(
        "project/<uuid:project_id>/delete/",
        views.delete_project,
        name="delete-project",
    ),
    path("tools/", views.tools_index, name="tools-index"),
    path(
        "tools/merge-video-audio/",
        views.merge_video_audio,
        name="merge-video-audio",
    ),
    path(
        "tools/merge-video-audio/<uuid:job_id>/",
        views.merge_video_audio_job,
        name="merge-video-audio-job",
    ),
    path(
        "tools/merge-video-audio/<uuid:job_id>/status/",
        views.merge_video_audio_status,
        name="merge-video-audio-status",
    ),
    path("project/<uuid:project_id>/audio/", views.edit_audio, name="edit-audio"),
    path("project/<uuid:project_id>/video/", views.upload_video, name="upload-video"),
    path("project/<uuid:project_id>/process/", views.process_audio, name="process-audio"),
    path("project/<uuid:project_id>/render/", views.render_project, name="render-project"),
    path(
        "project/<uuid:project_id>/editor-audio/",
        views.segment_editor_audio,
        name="segment-editor-audio",
    ),
    path(
        "project/<uuid:project_id>/segments/<int:position>/replacement/",
        views.replace_segment_audio,
        name="replace-segment-audio",
    ),
    path(
        "project/<uuid:project_id>/segments/<int:position>/editor-audio/",
        views.segment_editor_audio,
        name="segment-replacement-editor-audio",
    ),
    path(
        "project/<uuid:project_id>/segments/<int:position>/editor/",
        views.segment_editor_data,
        name="segment-editor-data",
    ),
    path(
        "project/<uuid:project_id>/segments/<int:position>/boundary/",
        views.save_segment_boundary,
        name="save-segment-boundary",
    ),
    path("project/<uuid:project_id>/status/", views.processing_status, name="processing-status"),
    path("project/<uuid:project_id>/", views.project_summary, name="project-summary"),
]
