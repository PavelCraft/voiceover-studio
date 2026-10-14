import json
import re
import shutil
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, HttpResponseNotAllowed, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _
from django.views.decorators.http import require_GET, require_POST

from .forms import AudioUploadForm, VideoAudioMergeForm, VideoUploadForm
from .models import ManualSegmentBoundary, Project, Segment, VideoAudioMergeJob
from .services.voiceover import (
    ProcessingError,
    cut_replacement_segment,
    ensure_editor_assets,
    ensure_replacement_assets,
)
from .workers import (
    start_merge_worker,
    start_project_render_worker,
    start_project_worker,
)

PROCESSING_STEP_KEYS = ["preparing", "transcribing", "aligning", "detecting", "cutting", "rendering"]
MERGE_STEP_KEYS = ["preparing", "merging", "finalizing"]


def processing_step_labels():
    return [
        _("Preparing audio"),
        _("Recognizing speech with Groq"),
        _("Matching phrases with recognized words"),
        _("Detecting pauses"),
        _("Cutting and fitting fragments"),
        _("Rendering the result with FFmpeg"),
    ]


def processing_stage_index(project):
    if project.processing_status == "complete":
        return len(PROCESSING_STEP_KEYS)
    try:
        return PROCESSING_STEP_KEYS.index(project.processing_step)
    except ValueError:
        return 0


def _project_for_user(request, project_id):
    return get_object_or_404(Project, id=project_id, owner=request.user)


def _project_worker_running(project):
    project_id = str(project.id).encode()
    for command_path in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            command = command_path.read_bytes()
        except OSError:
            continue
        if project_id in command and (
            b"process_project" in command or b"render_project" in command
        ):
            return True
    return False


def _recover_stale_processing(project):
    if project.processing_status == "processing" and not _project_worker_running(project):
        error = "Worker process is no longer running."
        updated = Project.objects.filter(
            pk=project.pk,
            processing_status="processing",
        ).update(
            processing_status="failed",
            processing_step="failed",
            processing_error=error,
        )
        if updated:
            project.processing_status = "failed"
            project.processing_step = "failed"
            project.processing_error = error
        else:
            project.refresh_from_db()
    return project



def home(request):
    return render(request, "studio/home.html")


@login_required
def upload_audio(request):
    form = AudioUploadForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        project = form.save(commit=False)
        project.owner = request.user
        project.save()
        return redirect("upload-video", project_id=project.id)
    return render(request, "studio/upload_audio.html", {"form": form})


@login_required
def edit_audio(request, project_id):
    project = _project_for_user(request, project_id)
    form = AudioUploadForm(request.POST or None, request.FILES or None, instance=project)
    if request.method == "POST" and form.is_valid():
        form.save()
        project.processing_status = "pending"
        project.processing_step = "pending"
        project.processing_error = ""
        project.output_file = ""
        project.save(update_fields=["processing_status", "processing_step", "processing_error", "output_file"])
        return redirect("upload-video", project_id=project.id)
    return render(request, "studio/upload_audio.html", {"form": form, "project": project})


@login_required
def upload_video(request, project_id):
    project = _project_for_user(request, project_id)
    form = VideoUploadForm(request.POST or None, request.FILES or None, instance=project)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, _("Files uploaded. You can start processing."))
        return redirect("project-summary", project_id=project.id)
    return render(request, "studio/upload_video.html", {"form": form, "project": project})


@login_required
def project_summary(request, project_id):
    project = _project_for_user(request, project_id)
    output_url = f"{settings.MEDIA_URL}{project.output_file}" if project.output_file else ""
    segments = list(project.segments.all())
    replacement_versions = {
        replacement.position: int(replacement.updated_at.timestamp() * 1000)
        for replacement in project.segment_replacements.all()
    }
    for segment in segments:
        segment.audio_version = replacement_versions.get(
            segment.position, f"{segment.audio_start}-{segment.audio_end}-{segment.source_duration}"
        )
    return render(request, "studio/project_summary.html", {
        "project": project,
        "segments": segments,
        "output_url": output_url,
        "processing_steps": processing_step_labels(),
        "processing_stage_index": processing_stage_index(project),
    })


@login_required
def process_audio(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = _project_for_user(request, project_id)
    if start_project_worker(project, clear_manual_boundaries=True):
        messages.info(request, _("Processing started. You may leave this page and return later."))
    else:
        messages.info(request, _("Processing is already running."))
    return redirect("project-summary", project_id=project.id)


@require_POST
@login_required
def render_project(request, project_id):
    project = _project_for_user(request, project_id)
    if not project.segments.exists():
        messages.error(request, _("Process and review the fragments first."))
    elif start_project_render_worker(project):
        messages.info(request, _("Final rendering started. You may leave this page and return later."))
    else:
        messages.info(request, _("Processing is already running."))
    return redirect("project-summary", project_id=project.id)


@login_required
def processing_status(request, project_id):
    project = _project_for_user(request, project_id)
    project = _recover_stale_processing(project)
    return JsonResponse({
        "status": project.processing_status,
        "stage_index": processing_stage_index(project),
        "complete": project.processing_status == "complete",
        "failed": project.processing_status == "failed",
        "error": project.processing_error,
    })


def _context_tail(text, length=180):
    return ("…" + text[-length:]) if len(text) > length else text


def _context_head(text, length=180):

    return (text[:length] + "…") if len(text) > length else text
@require_POST
@login_required
def replace_segment_audio(request, project_id, position):
    project = _project_for_user(request, project_id)
    get_object_or_404(Segment, project=project, position=position)
    if project.processing_status in {"queued", "processing"}:
        messages.error(request, _("Processing is already running."))
        return redirect("project-summary", project_id=project.id)
    uploaded = request.FILES.get("audio_file")
    if not uploaded:
        messages.error(request, _("Select an audio file to replace the fragment."))
        return redirect("project-summary", project_id=project.id)
    replacement = project.segment_replacements.filter(position=position).first()
    if replacement:
        replacement.audio_file = uploaded
        replacement.audio_start = 0
        replacement.audio_end = None
        replacement.save()
    else:
        replacement = project.segment_replacements.create(position=position, audio_file=uploaded)
    project.manual_boundaries.filter(position=position).delete()
    try:
        cut_replacement_segment(project, replacement)
    except ProcessingError as error:
        messages.error(request, str(error))
    else:
        messages.success(request, _("The fragment audio has been replaced."))
    return redirect("project-summary", project_id=project.id)



@require_GET
@login_required
def segment_editor_data(request, project_id, position):
    project = _project_for_user(request, project_id)
    segment = get_object_or_404(Segment, project=project, position=position)
    replacement = project.segment_replacements.filter(position=position).first()
    assets = ensure_replacement_assets(project, replacement) if replacement else ensure_editor_assets(project)
    asset_version = Path(settings.MEDIA_ROOT, assets["source"]).stat().st_mtime_ns
    previous = project.segments.filter(position__lt=position).order_by("-position").first()
    following = project.segments.filter(position__gt=position).order_by("position").first()
    override = project.manual_boundaries.filter(position=position).first()
    if replacement:
        start = float(replacement.audio_start or 0)
        end = float(replacement.audio_end or assets["duration"])
        audio_url = reverse(
            "segment-replacement-editor-audio",
            kwargs={"project_id": project.id, "position": position},
        )
    else:
        start = segment.audio_start
        end = segment.audio_end
        if start is None:
            start = previous.audio_end if previous and previous.audio_end is not None else None
        if end is None:
            end = following.audio_start if following and following.audio_start is not None else None
        audio_url = reverse("segment-editor-audio", kwargs={"project_id": project.id})
    focus = start if start is not None else (end if end is not None else 0)
    return JsonResponse({
        "position": position,
        "text": segment.text,
        "previous_text": _context_tail(previous.text) if previous else "",
        "next_text": _context_head(following.text) if following else "",
        "audio_start": start,
        "audio_end": end,
        "focus": focus,
        "duration": assets["duration"],
        "audio_url": f"{request.build_absolute_uri(audio_url)}?v={asset_version}",
        "peaks_url": f"{settings.MEDIA_URL}{assets['peaks']}?v={asset_version}",
        "manual": bool(replacement or (override and not override.skipped)),
        "skipped": bool(override and override.skipped),
        "save_url": request.build_absolute_uri(
            request.path.replace("editor/", "boundary/")
        ),
    })


def _file_range(path, start, length, block_size=64 * 1024):
    with path.open("rb") as source:
        source.seek(start)
        remaining = length
        while remaining > 0:
            chunk = source.read(min(block_size, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


@require_GET
@login_required
def segment_editor_audio(request, project_id, position=None):
    project = _project_for_user(request, project_id)
    segment_path = None
    segment_number = request.GET.get("segment")
    if position is None and segment_number:
        segment_path = Path(get_object_or_404(Segment, project=project, position=segment_number).audio_file.path)
    if position is None:
        if segment_path is None:
            assets = ensure_editor_assets(project)
    else:
        replacement = get_object_or_404(project.segment_replacements, position=position)
        assets = ensure_replacement_assets(project, replacement)
    path = segment_path or Path(settings.MEDIA_ROOT) / assets["source"]
    size = path.stat().st_size
    range_header = request.headers.get("Range", "")
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        response = FileResponse(path.open("rb"), content_type="audio/wav")
        response["Content-Length"] = size
        response["Accept-Ranges"] = "bytes"
        return response
    first, last = match.groups()
    if first:
        start = int(first)
        end = int(last) if last else size - 1
    elif last:
        suffix_length = min(int(last), size)
        start = size - suffix_length
        end = size - 1
    else:
        start, end = 0, size - 1
    if start >= size or start < 0 or end < start:
        response = StreamingHttpResponse(status=416)
        response["Content-Range"] = f"bytes */{size}"
        return response
    end = min(end, size - 1)
    length = end - start + 1
    response = StreamingHttpResponse(
        _file_range(path, start, length),
        status=206,
        content_type="audio/wav",
    )
    response["Content-Length"] = length
    response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = f'inline; filename="{path.name}"'
    return response


@require_POST
@login_required
def save_segment_boundary(request, project_id, position):
    project = _project_for_user(request, project_id)
    segment = get_object_or_404(Segment, project=project, position=position)
    replacement = project.segment_replacements.filter(position=position).first()
    if project.processing_status in {"queued", "processing"}:
        return JsonResponse({"error": _("Processing is already running.")}, status=409)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": _("Invalid editor data.")}, status=400)
    skipped = bool(payload.get("skipped"))
    if skipped:
        start = end = None
    else:
        try:
            start = float(payload["audio_start"])
            end = float(payload["audio_end"])
        except (KeyError, TypeError, ValueError):
            return JsonResponse({"error": _("Select the beginning and end of the phrase.")}, status=400)
        duration = (ensure_replacement_assets(project, replacement) if replacement else ensure_editor_assets(project))["duration"]
        if start < 0 or end > duration or start >= end:
            return JsonResponse({"error": _("The selected phrase boundaries are invalid.")}, status=400)
    if replacement:
        if skipped:
            segment.status = "skipped"
            segment.audio_file = ""
            segment.save(update_fields=["status", "audio_file"])
        else:
            replacement.audio_start = start
            replacement.audio_end = end
            replacement.save(update_fields=["audio_start", "audio_end", "updated_at"])
            try:
                cut_replacement_segment(project, replacement)
            except ProcessingError as error:
                return JsonResponse({"error": str(error)}, status=400)
        return JsonResponse({
            "ok": True,
            "processing": False,
            "redirect_url": request.build_absolute_uri(request.path.split("segments/")[0]),
        })

    ManualSegmentBoundary.objects.update_or_create(
        project=project,
        position=position,
        defaults={"audio_start": start, "audio_end": end, "skipped": skipped},
    )
    start_project_worker(
        project,
        reuse_transcription=True,
        realign_from_position=position,
    )
    return JsonResponse({
        "ok": True,
        "processing": True,
        "status_url": request.build_absolute_uri(reverse("processing-status", kwargs={"project_id": project.id})),
        "redirect_url": request.build_absolute_uri(
            request.path.split("segments/")[0]
        ),
    })



@login_required
def dashboard(request):
    projects = Project.objects.filter(owner=request.user).order_by("-created_at")
    return render(request, "studio/dashboard.html", {"projects": projects})


@login_required
def delete_project(request, project_id):
    project = _project_for_user(request, project_id)
    project = _recover_stale_processing(project)
    if request.method == "POST":
        if project.processing_status in {"queued", "processing"}:
            messages.error(request, _("Wait until project processing is complete before deleting it."))
            return redirect("dashboard")
        project_directory = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id)
        project.delete()
        if project_directory.is_dir() and project_directory.parent == Path(settings.MEDIA_ROOT) / "jobs":
            shutil.rmtree(project_directory)
        messages.success(request, _("The project and all its files were deleted."))
        return redirect("dashboard")
    return render(request, "studio/delete_project.html", {"project": project})


def merge_step_labels():
    return [
        _("Checking the uploaded files"),
        _("Combining video and audio with FFmpeg"),
        _("Preparing the result for download"),
    ]


def merge_stage_index(job):
    if job.processing_status == "complete":
        return len(MERGE_STEP_KEYS)
    try:
        return MERGE_STEP_KEYS.index(job.processing_step)
    except ValueError:
        return 0


def tools_index(request):
    return render(request, "studio/tools_index.html")


def merge_video_audio(request):
    form = VideoAudioMergeForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        job = form.save()
        start_merge_worker(job)
        return redirect("merge-video-audio-job", job_id=job.id)
    return render(request, "studio/merge_video_audio.html", {"form": form})


def merge_video_audio_job(request, job_id):
    job = get_object_or_404(VideoAudioMergeJob, id=job_id)
    output_url = f"{settings.MEDIA_URL}{job.output_file}" if job.output_file else ""
    return render(request, "studio/merge_video_audio_job.html", {
        "job": job,
        "output_url": output_url,
        "processing_steps": merge_step_labels(),
        "processing_stage_index": merge_stage_index(job),
    })


def merge_video_audio_status(request, job_id):
    job = get_object_or_404(VideoAudioMergeJob, id=job_id)
    return JsonResponse({
        "status": job.processing_status,
        "stage_index": merge_stage_index(job),
        "complete": job.processing_status == "complete",
        "failed": job.processing_status == "failed",
    })
