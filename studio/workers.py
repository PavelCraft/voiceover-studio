import subprocess
import sys
from pathlib import Path

from django.conf import settings
from django.db import transaction

from studio.models import MediaToolJob, PhraseTimingProject, ProcessingJob, Project, TakesProject, VideoAudioMergeJob
from studio.services.storage import (
    estimate_from_fields,
    estimate_media_tool_processing,
    estimate_voiceover_processing,
    release_storage,
    reserve_storage,
)


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
    clear_replacements=False,
    preserve_following=True,
    timing_only_positions=None,
):
    return bool(_enqueue_project_job(
        project,
        ProcessingJob.KIND_PROCESS,
        {
            "reuse_transcription": bool(reuse_transcription),
            "realign_from_position": realign_from_position,
            "preserve_following": bool(preserve_following),
            "timing_only_positions": timing_only_positions,
        },
        "cutting" if timing_only_positions is not None else ("aligning" if reuse_transcription else "pending"),
        clear_manual_boundaries=clear_manual_boundaries,
        clear_replacements=clear_replacements,
        required_bytes=estimate_voiceover_processing(project),
    ))


def start_project_render_worker(project):
    return bool(_enqueue_project_job(
        project, ProcessingJob.KIND_RENDER, {}, "rendering",
        required_bytes=estimate_voiceover_processing(project, render=True),
    ))


def _enqueue_project_job(
    project, kind, payload, step,
    clear_manual_boundaries=False, clear_replacements=False,
    required_bytes=0,
):
    reservation_key = f"voiceover:{project.pk}"
    with transaction.atomic():
        locked = Project.objects.select_for_update().get(pk=project.pk)
        if locked.jobs.filter(status__in={"queued", "running"}).exists():
            return None
        reserve_storage(reservation_key, required_bytes)
        if clear_manual_boundaries:
            locked.manual_boundaries.all().delete()
        if clear_replacements:
            for replacement in locked.segment_replacements.all():
                replacement.audio_file.delete(save=False)
                replacement.delete()
        locked.processing_status = "queued"
        locked.processing_step = step
        locked.processing_error = ""
        locked.output_file = ""
        locked.export_manifest = {}
        locked.save()
        job = ProcessingJob.objects.create(
            project=locked,
            kind=kind,
            payload=payload,
            current_step=step,
        )
    if not settings.VOICEOVER_AUTOSTART_WORKER:
        return job
    try:
        pid = _spawn(
            ["run_voiceover_worker", "--once", "--job", str(job.pk)],
            settings.MEDIA_ROOT / "worker.log",
        )
        ProcessingJob.objects.filter(pk=job.pk).update(worker_pid=pid)
    except Exception as error:
        release_storage(reservation_key)
        ProcessingJob.objects.filter(pk=job.pk).update(status="failed", error=str(error))
        Project.objects.filter(pk=project.pk).update(
            processing_status="failed", processing_error=str(error)
        )
        raise
    return job


def start_merge_worker(job):
    reservation_key = f"merge:{job.pk}"
    if job.processing_status in {"queued", "processing"}:
        return False
    reserve_storage(
        reservation_key,
        estimate_from_fields(job.video_file, job.audio_file, multiplier=1),
    )
    claimed = VideoAudioMergeJob.objects.filter(pk=job.pk).exclude(
        processing_status__in={"queued", "processing"}
    ).update(
        processing_status="queued",
        processing_step="preparing",
        processing_error="",
    )
    if not claimed:
        release_storage(reservation_key)
        return False
    try:
        _spawn(
            ["merge_video_audio", str(job.pk)],
            settings.MEDIA_ROOT / "tools" / "video-audio" / str(job.pk) / "worker.log",
        )
    except Exception as error:
        release_storage(reservation_key)
        VideoAudioMergeJob.objects.filter(pk=job.pk).update(
            processing_status="failed",
            processing_error=str(error),
        )
        raise
    return True


def start_media_tool_worker(job):
    reservation_key = f"media-tool:{job.pk}"
    if job.processing_status in {"queued", "processing"}:
        return False
    reserve_storage(
        reservation_key,
        estimate_media_tool_processing(job),
    )
    claimed = MediaToolJob.objects.filter(pk=job.pk).exclude(
        processing_status__in={"queued", "processing"}
    ).update(processing_status="queued", processing_step="preparing", processing_error="")
    if not claimed:
        release_storage(reservation_key)
        return False
    try:
        _spawn(
            ["process_media_tool", str(job.pk)],
            settings.MEDIA_ROOT / "tools" / "jobs" / str(job.pk) / "worker.log",
        )
    except Exception as error:
        release_storage(reservation_key)
        MediaToolJob.objects.filter(pk=job.pk).update(processing_status="failed", processing_error=str(error))
        raise
    return True


def start_phrase_timing_worker(project):
    reservation_key = f"phrase-timing:{project.pk}"
    if project.processing_status in {"queued", "processing"}:
        return False
    reserve_storage(
        reservation_key,
        estimate_from_fields(project.video_file, multiplier=1),
    )
    claimed = PhraseTimingProject.objects.filter(pk=project.pk).exclude(
        processing_status__in={"queued", "processing"}
    ).update(processing_status="queued", processing_step="preparing", processing_error="")
    if not claimed:
        release_storage(reservation_key)
        return False
    try:
        _spawn(
            ["process_phrase_timing", str(project.pk)],
            settings.MEDIA_ROOT / "tools" / "phrase-timing" / str(project.pk) / "worker.log",
        )
    except Exception as error:
        release_storage(reservation_key)
        PhraseTimingProject.objects.filter(pk=project.pk).update(
            processing_status="failed", processing_error=str(error),
        )
        raise
    return True


def start_takes_worker(project):
    reservation_key = f"takes:{project.pk}"
    if project.processing_status in {"queued", "processing"}:
        return False
    reserve_storage(
        reservation_key,
        estimate_from_fields(
            project.video_file,
            *(item.media_file for item in project.recordings.all()),
        ),
    )
    claimed = TakesProject.objects.filter(pk=project.pk).exclude(
        processing_status__in={"queued", "processing"}
    ).update(processing_status="queued", processing_step="preparing", processing_error="")
    if not claimed:
        release_storage(reservation_key)
        return False
    try:
        _spawn(
            ["process_takes_project", str(project.pk)],
            settings.MEDIA_ROOT / "takes" / str(project.pk) / "worker.log",
        )
    except Exception as error:
        release_storage(reservation_key)
        TakesProject.objects.filter(pk=project.pk).update(processing_status="failed", processing_error=str(error))
        raise
    return True
