import subprocess
import sys
from pathlib import Path

from django.conf import settings

from studio.models import Project, VideoAudioMergeJob


def _spawn(arguments, log_path):
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    with Path(log_path).open("ab") as log:
        process = subprocess.Popen(
            [sys.executable, str(settings.BASE_DIR / "manage.py"), *arguments],
            cwd=settings.BASE_DIR,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return process.pid


def start_project_worker(
    project,
    reuse_transcription=False,
    realign_from_position=None,
    clear_manual_boundaries=False,
):
    claimed = Project.objects.filter(pk=project.pk).exclude(
        processing_status__in={"queued", "processing"}
    ).update(
        processing_status="queued",
        processing_step="aligning" if reuse_transcription else "pending",
        processing_error="",
    )
    if not claimed:
        return False
    if clear_manual_boundaries:
        project.manual_boundaries.all().delete()
    arguments = ["process_project", str(project.pk)]
    if reuse_transcription:
        arguments.append("--reuse-transcription")
    if realign_from_position is not None:
        arguments.extend(["--realign-from", str(realign_from_position)])
    try:
        _spawn(
            arguments,
            settings.MEDIA_ROOT / "jobs" / str(project.pk) / "worker.log",
        )
    except Exception as error:
        Project.objects.filter(pk=project.pk).update(
            processing_status="failed",
            processing_error=str(error),
        )
        raise
    return True


def start_project_render_worker(project):
    claimed = Project.objects.filter(pk=project.pk).exclude(
        processing_status__in={"queued", "processing"}
    ).update(
        processing_status="queued",
        processing_step="rendering",
        processing_error="",
        output_file="",
    )
    if not claimed:
        return False
    try:
        _spawn(
            ["render_project", str(project.pk)],
            settings.MEDIA_ROOT / "jobs" / str(project.pk) / "worker.log",
        )
    except Exception as error:
        Project.objects.filter(pk=project.pk).update(
            processing_status="failed",
            processing_error=str(error),
        )
        raise
    return True


def start_merge_worker(job):
    claimed = VideoAudioMergeJob.objects.filter(pk=job.pk).exclude(
        processing_status__in={"queued", "processing"}
    ).update(
        processing_status="queued",
        processing_step="preparing",
        processing_error="",
    )
    if not claimed:
        return False
    try:
        _spawn(
            ["merge_video_audio", str(job.pk)],
            settings.MEDIA_ROOT / "tools" / "video-audio" / str(job.pk) / "worker.log",
        )
    except Exception as error:
        VideoAudioMergeJob.objects.filter(pk=job.pk).update(
            processing_status="failed",
            processing_error=str(error),
        )
        raise
    return True
