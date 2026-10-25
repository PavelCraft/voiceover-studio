from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from studio.models import ProcessingJob
from studio.services.voiceover import process_project, render_project_result
from studio.services.storage import release_storage


def claim_job(job_id=None):
    with transaction.atomic():
        jobs = ProcessingJob.objects.select_for_update().filter(status="queued")
        if job_id:
            jobs = jobs.filter(pk=job_id)
        job = jobs.order_by("created_at").first()
        if not job:
            return None
        job.status = "running"
        job.attempts += 1
        job.started_at = timezone.now()
        job.heartbeat_at = job.started_at
        job.error = ""
        job.save(update_fields=["status", "attempts", "started_at", "heartbeat_at", "error"])
        return job


def execute_job(job):
    project = job.project
    try:
        if job.cancel_requested:
            raise InterruptedError("Processing was cancelled")
        if job.kind == ProcessingJob.KIND_PROCESS:
            process_project(
                project,
                reuse_transcription=bool(job.payload.get("reuse_transcription")),
                realign_from_position=job.payload.get("realign_from_position"),
                preserve_following=bool(job.payload.get("preserve_following", True)),
                timing_only_positions=job.payload.get("timing_only_positions"),
            )
        else:
            render_project_result(project)
        job.status = "complete"
        job.progress = 100
        job.current_step = "complete"
    except InterruptedError as error:
        job.status = "cancelled"
        job.error = str(error)
        project.processing_status = "cancelled"
        project.processing_error = str(error)
        project.save(update_fields=["processing_status", "processing_error"])
    except Exception as error:
        job.status = "failed"
        job.error = str(error)
        project.processing_status = "failed"
        project.processing_error = str(error)
        project.save(update_fields=["processing_status", "processing_error"])
        raise
    finally:
        job.finished_at = timezone.now()
        job.heartbeat_at = job.finished_at
        job.save(update_fields=["status", "progress", "current_step", "error", "finished_at", "heartbeat_at"])
        release_storage(f"voiceover:{project.pk}")


def recover_stale_jobs(max_age_minutes=60):
    cutoff = timezone.now() - timedelta(minutes=max_age_minutes)
    stale = ProcessingJob.objects.filter(status="running", heartbeat_at__lt=cutoff)
    count = 0
    for job in stale:
        if job.attempts < job.max_attempts:
            job.status = "queued"
            job.worker_pid = None
            job.error = "Worker stopped; job returned to the queue."
            job.save(update_fields=["status", "worker_pid", "error"])
            ProjectClass = job.project.__class__
            ProjectClass.objects.filter(pk=job.project_id).update(processing_status="queued")
        else:
            job.status = "failed"
            job.error = "Worker stopped and retry limit was reached."
            job.finished_at = timezone.now()
            job.save(update_fields=["status", "error", "finished_at"])
            ProjectClass = job.project.__class__
            ProjectClass.objects.filter(pk=job.project_id).update(
                processing_status="failed", processing_error=job.error
            )
            release_storage(f"voiceover:{job.project_id}")
        count += 1
    return count
