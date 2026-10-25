import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from studio.models import Project


def purge_expired_projects(dry_run=False):
    now, expired = timezone.now(), []
    projects = Project.objects.exclude(retention_days=0).exclude(
        processing_status__in={"queued", "processing"}
    )
    jobs_root = (Path(settings.MEDIA_ROOT) / "jobs").resolve()
    for project in projects:
        if project.created_at + timedelta(days=project.retention_days) > now:
            continue
        expired.append(str(project.pk))
        if dry_run:
            continue
        directory = (jobs_root / str(project.pk)).resolve()
        project.delete()
        if directory.is_dir() and directory.parent == jobs_root:
            shutil.rmtree(directory)
    return expired
