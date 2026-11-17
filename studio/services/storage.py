from __future__ import annotations

import os
import shutil
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.db.models import Sum
from django.utils import timezone

from studio.models import StorageReservation


class StorageLimitError(RuntimeError):
    pass


def directory_size(path: Path) -> int:
    total = 0
    try:
        for root, _directories, files in os.walk(path):
            for name in files:
                try:
                    total += (Path(root) / name).stat().st_size
                except FileNotFoundError:
                    pass
    except FileNotFoundError:
        return 0
    return total


def media_usage() -> int:
    return directory_size(Path(settings.MEDIA_ROOT))


def reserved_bytes(exclude_key: str | None = None) -> int:
    reservations = StorageReservation.objects.all()
    if exclude_key:
        reservations = reservations.exclude(key=exclude_key)
    return int(reservations.aggregate(total=Sum("reserved_bytes"))["total"] or 0)


def ensure_storage_available(required_bytes: int = 0, exclude_key: str | None = None) -> None:
    required = max(0, int(required_bytes))
    root = Path(settings.MEDIA_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    used = media_usage()
    reserved = reserved_bytes(exclude_key=exclude_key)
    if used + reserved + required > settings.VOICEOVER_MEDIA_LIMIT_BYTES:
        raise StorageLimitError(
            "The studio storage is temporarily full. Delete completed projects or try again later."
        )
    free = shutil.disk_usage(settings.VOICEOVER_DISK_CHECK_PATH).free
    if free - required < settings.VOICEOVER_DISK_RESERVE_BYTES:
        raise StorageLimitError(
            "The server does not have enough reserved disk space for this operation."
        )


@transaction.atomic
def reserve_storage(key: str, required_bytes: int) -> None:
    existing = StorageReservation.objects.select_for_update().filter(key=key).first()
    ensure_storage_available(required_bytes, exclude_key=key if existing else None)
    StorageReservation.objects.update_or_create(
        key=key,
        defaults={"reserved_bytes": max(0, int(required_bytes))},
    )


def release_storage(key: str) -> None:
    StorageReservation.objects.filter(key=key).delete()


def purge_stale_reservations(max_age_hours: int = 24) -> int:
    cutoff = timezone.now() - timedelta(hours=max_age_hours)
    deleted, _details = StorageReservation.objects.filter(updated_at__lt=cutoff).delete()
    return deleted


def file_size(field) -> int:
    try:
        return Path(field.path).stat().st_size if field else 0
    except (FileNotFoundError, OSError, ValueError):
        return 0


def estimate_voiceover_processing(project, render=False) -> int:
    video = file_size(project.video_file)
    audio = file_size(project.audio_file)
    if render:
        return max(64 * 1024**2, video if project.output_mode == project.OUTPUT_VIDEO else audio)
    return max(128 * 1024**2, audio * 4)


def estimate_from_fields(*fields, multiplier=2, minimum=64 * 1024**2) -> int:
    return max(minimum, sum(file_size(field) for field in fields) * multiplier)


def estimate_media_tool_processing(job) -> int:
    base = estimate_from_fields(*(item.media_file for item in job.inputs.all()))
    if job.kind != "edit_audio" or job.options.get("phase") != "render":
        return base
    tracks = (job.result_manifest or {}).get("tracks", [])
    duration = sum(float(item.get("duration", 0) or 0) for item in tracks)
    for operation in job.options.get("operations", []):
        try:
            source_duration = float(operation["end"]) - float(operation["start"])
            if operation.get("type") == "cut":
                duration -= source_duration
            elif operation.get("type") == "speed":
                duration += source_duration / float(operation["speed"]) - source_duration
            elif operation.get("type") == "silence":
                duration += float(operation["duration"]) - source_duration
        except (KeyError, TypeError, ValueError, ZeroDivisionError):
            return base
    try:
        duration += float(job.options.get("trailing_silence", 0) or 0)
    except (TypeError, ValueError):
        return base
    output_format = job.options.get("output_format", "mp3")
    if output_format == "wav":
        output_bytes = max(0, duration) * 44_100 * 2 * 3
    else:
        bitrate = int(job.options.get("mp3_bitrate", 192) or 192)
        output_bytes = max(0, duration) * bitrate * 1000 / 8
    temporary_bytes = duration * 44_100 * 2 * 2 if job.inputs.count() > 1 else 0
    return max(base, int((output_bytes + temporary_bytes) * 1.1))
