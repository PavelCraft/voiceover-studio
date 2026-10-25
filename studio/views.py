import json
import mimetypes
import re
import shutil
import uuid
import zipfile
from pathlib import Path

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse, HttpResponseNotAllowed, JsonResponse, StreamingHttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.translation import gettext as _, gettext_lazy
from django.views.decorators.http import require_GET, require_POST

from .forms import (
    ALLOWED_AUDIO, AudioUploadForm, LocalizationProjectForm,
    LocalizationVersionForm, PhraseTimingToolForm, ProjectTimecodesForm, TOOL_FORMS,
    TakesProjectForm, TranscribeAudioToolForm, VideoAudioMergeForm, VideoUploadForm,
    VoiceoverAudioForm,
)
from .models import (
    LocalizationProject, LocalizationVersion, ManualSegmentBoundary,
    MediaToolInput, MediaToolJob, PhraseTimingProject, ProcessingJob, Project, Segment,
    TakeCandidate, TakeRecording, TakesProject, VideoAudioMergeJob,
)
from .services.voiceover import (
    ProcessingError,
    cut_replacement_segment,
    ensure_editor_assets,
    ensure_replacement_assets,
)
from .services.phrase_timing_setup import initialize_phrase_timing_project
from .services.reprocessing import (
    build_change_plan,
    merge_change_plans,
    remap_project_annotations,
)
from .services.storage import release_storage, reserve_storage
from .workers import (
    start_media_tool_worker,
    start_merge_worker,
    start_phrase_timing_worker,
    start_project_render_worker,
    start_project_worker,
    start_takes_worker,
)
from voiceover_core.diagnostics import DiagnosticOptions, diagnose_segments
from voiceover_core.subtitles import SubtitleParseError, parse_script
from voiceover_core.timing import export_timed_phrases, normalize_timed_phrases, parse_editable_timing_text, split_script
from voiceover_core.exports import create_clips_archive

PROCESSING_STEP_KEYS = ["preparing", "transcribing", "aligning", "detecting", "cutting", "rendering"]
MERGE_STEP_KEYS = ["preparing", "merging", "finalizing"]
PROCESSING_STEP_LABELS = {
    "preparing": gettext_lazy("Preparing audio"),
    "transcribing": gettext_lazy("Recognizing speech with Groq"),
    "aligning": gettext_lazy("Matching phrases with recognized words"),
    "detecting": gettext_lazy("Detecting pauses"),
    "cutting": gettext_lazy("Cutting and fitting fragments"),
    "rendering": gettext_lazy("Rendering the result with FFmpeg"),
}


def project_processing_step_keys(project, job=None):
    if project.processing_step == "rendering" or (job and job.kind == ProcessingJob.KIND_RENDER):
        return ["rendering"]
    payload = job.payload if job and job.kind == ProcessingJob.KIND_PROCESS else {}
    if payload.get("timing_only_positions") is not None:
        return ["cutting"]
    if payload.get("reuse_transcription"):
        return ["aligning", "detecting", "cutting"]
    return PROCESSING_STEP_KEYS[:5]


def processing_step_labels(project=None, job=None):
    keys = project_processing_step_keys(project, job) if project else PROCESSING_STEP_KEYS
    return [PROCESSING_STEP_LABELS[key] for key in keys]


def processing_stage_index(project, job=None):
    keys = project_processing_step_keys(project, job)
    if project.processing_status == "complete":
        return len(keys)
    try:
        return keys.index(project.processing_step)
    except ValueError:
        return 0


def _project_for_user(request, project_id):
    return get_object_or_404(Project, id=project_id, owner=request.user)


def _delete_replaced_file(path, expected_parent):
    if not path:
        return
    try:
        candidate = Path(path).resolve()
        parent = Path(expected_parent).resolve()
        if candidate.is_file() and parent in candidate.parents:
            candidate.unlink()
    except (OSError, ValueError):
        pass


def _recover_stale_processing(project):
    from .services.job_queue import recover_stale_jobs

    recover_stale_jobs()
    project.refresh_from_db()
    return project



def home(request):
    return render(request, "studio/home.html")


def transcribe_audio(request, job_id=None):
    job = _media_tool_job_for_request(request, job_id) if job_id else None
    if job and job.kind != MediaToolJob.KIND_TRANSCRIBE:
        raise Http404
    form = TranscribeAudioToolForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        job = _create_media_tool_job(request, MediaToolJob.KIND_TRANSCRIBE, form.cleaned_data)
        start_media_tool_worker(job)
        return redirect("transcribe-audio-job", job_id=job.id)
    return render(request, "studio/transcribe_audio.html", {
        "form": form,
        "job": job,
        "txt_url": f"{settings.MEDIA_URL}{job.result_manifest.get('txt', '')}" if job else "",
        "docx_url": f"{settings.MEDIA_URL}{job.result_manifest.get('docx', '')}" if job else "",
    })


def phrase_timing(request):
    form = PhraseTimingToolForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        project = form.save(commit=False)
        project.owner = request.user if request.user.is_authenticated else None
        try:
            automatic = initialize_phrase_timing_project(project)
        except ValueError as error:
            form.add_error("source_text", str(error))
        else:
            if automatic:
                start_phrase_timing_worker(project)
            return redirect("phrase-timing-editor", project_id=project.id)
    return render(request, "studio/phrase_timing.html", {"form": form})


def _phrase_timing_project_for_request(request, project_id):
    project = get_object_or_404(PhraseTimingProject, id=project_id)
    if project.owner_id and (not request.user.is_authenticated or request.user.id != project.owner_id):
        raise Http404
    return project


def phrase_timing_editor(request, project_id):
    project = _phrase_timing_project_for_request(request, project_id)
    scenario_project = Project.objects.filter(timing_project=project).first()
    if project.processing_status != "complete":
        return render(request, "studio/phrase_timing_processing.html", {
            "timing_project": project, "scenario_project": scenario_project,
        })
    return render(request, "studio/phrase_timing_editor.html", {
        "timing_project": project, "scenario_project": scenario_project,
    })


@require_GET
def phrase_timing_status(request, project_id):
    project = _phrase_timing_project_for_request(request, project_id)
    return JsonResponse({
        "status": project.processing_status,
        "step": project.processing_step,
        "error": project.processing_error,
    })


@require_GET
def phrase_timing_media(request, project_id):
    project = _phrase_timing_project_for_request(request, project_id)
    path = Path(project.video_file.path)
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return _range_file_response(request, path, content_type)


phrase_timing_video = phrase_timing_media


@require_POST
def save_phrase_timing(request, project_id):
    project = _phrase_timing_project_for_request(request, project_id)
    try:
        payload = json.loads(request.body)
        duration = payload.get("duration")
        duration = float(duration) if duration not in (None, "") else None
        raw_phrases = payload.get("phrases", [])
        phrases = normalize_timed_phrases(raw_phrases, duration)
        for normalized, raw in zip(phrases, raw_phrases):
            for key in ("words", "issues", "source"):
                if key in raw:
                    normalized[key] = raw[key]
        review_items = []
        for item in payload.get("review_items", project.review_items):
            if item.get("kind") != "unrecognized_speech":
                continue
            review_items.append({
                "id": str(item.get("id", ""))[:80],
                "kind": "unrecognized_speech",
                "start": max(0.0, round(float(item.get("start", 0)), 3)),
                "end": max(0.0, round(float(item.get("end", 0)), 3)),
                "resolved": bool(item.get("resolved")),
            })
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    if not phrases:
        return JsonResponse({"ok": False, "error": str(_("The text contains no phrases."))}, status=400)
    project.phrases = phrases
    project.source_text = "\n".join(item["text"] for item in phrases)
    project.video_duration = duration
    project.review_items = review_items
    project.save(update_fields=["phrases", "source_text", "review_items", "video_duration", "updated_at"])
    return JsonResponse({"ok": True, "phrases": phrases})


@require_POST
def resegment_phrase_timing(request, project_id):
    project = _phrase_timing_project_for_request(request, project_id)
    try:
        payload = json.loads(request.body)
        source_text = str(payload.get("text", "")).strip()
        duration = payload.get("duration")
        duration = float(duration) if duration not in (None, "") else project.video_duration
        phrases = parse_editable_timing_text(source_text, duration)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        return JsonResponse({"ok": False, "error": str(error)}, status=400)
    if not phrases:
        return JsonResponse({"ok": False, "error": str(_("The text contains no phrases."))}, status=400)
    project.source_text = "\n".join(item["text"] for item in phrases)
    project.phrases = phrases
    project.video_duration = duration
    project.save(update_fields=["source_text", "phrases", "video_duration", "updated_at"])
    return JsonResponse({"ok": True, "phrases": phrases})


def export_phrase_timing(request, project_id, export_format):
    project = _phrase_timing_project_for_request(request, project_id)
    if export_format not in {"txt", "srt", "vtt"}:
        raise Http404
    try:
        content = export_timed_phrases(
            project.phrases,
            export_format,
            project.video_duration,
            include_unmarked=request.GET.get("include_unmarked") == "1",
        )
    except ValueError as error:
        return HttpResponse(str(error), status=400)
    content_type = "text/plain; charset=utf-8" if export_format == "txt" else "text/vtt; charset=utf-8" if export_format == "vtt" else "application/x-subrip; charset=utf-8"
    response = HttpResponse(content, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="phrase-timecodes.{export_format}"'
    return response


@require_POST
def download_transcription(request, job_id, export_format):
    from .services.tool_processing import build_docx_bytes

    job = _media_tool_job_for_request(request, job_id)
    if job.kind != MediaToolJob.KIND_TRANSCRIBE or job.processing_status != "complete":
        raise Http404
    text = request.POST.get("text", "").strip()
    if not text:
        return HttpResponse(_("The transcript is empty."), status=400)
    if export_format == "txt":
        response = HttpResponse(text + "\n", content_type="text/plain; charset=utf-8")
        filename = "transcript.txt"
    elif export_format == "docx":
        response = HttpResponse(
            build_docx_bytes(text),
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        filename = "transcript.docx"
    else:
        raise Http404
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response


@login_required
def project_scenario_select(request):
    return render(request, "studio/scenario_select.html")


@login_required
def localization_scenario(request):
    form = LocalizationProjectForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        project = form.save(commit=False)
        project.owner = request.user
        project.save()
        labels = dict(form.fields["languages"].choices)
        for code in form.cleaned_data["languages"]:
            LocalizationVersion.objects.create(
                localization=project, language_code=code, language_name=str(labels[code])
            )
        return redirect("localization-detail", project_id=project.id)
    return render(request, "studio/localization_scenario.html", {"form": form})


@login_required
def takes_scenario(request):
    form = TakesProjectForm(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        project = form.save(commit=False)
        project.owner = request.user
        project.save()
        for position, upload in enumerate(form.cleaned_data["recordings"], 1):
            TakeRecording.objects.create(takes_project=project, position=position, media_file=upload)
        start_takes_worker(project)
        return redirect("takes-detail", project_id=project.id)
    return render(request, "studio/takes_scenario.html", {"form": form})


TOOL_PROTOTYPES = {
    "replace-fragment": {
        "title": gettext_lazy("Replace a voiceover fragment"),
        "description": gettext_lazy("Replace one phrase in a finished file and check the transition before export."),
        "key": "replace-fragment",
    },
    "split-phrases": {
        "title": gettext_lazy("Split voiceover into phrases"),
        "description": gettext_lazy("Turn a long recording into a neatly named set of separate phrases."),
        "key": "split-phrases",
    },
    "fit-subtitles": {
        "title": gettext_lazy("Fit audio to subtitles"),
        "description": gettext_lazy("Place ready audio into SRT or VTT intervals and spot phrases that need attention."),
        "key": "fit-subtitles",
    },
    "prepare-voiceover": {
        "title": gettext_lazy("Prepare voiceover"),
        "description": gettext_lazy("Clean and level one recording or a whole set of phrase files."),
        "key": "prepare-voiceover",
    },
    "check-durations": {
        "title": gettext_lazy("Check phrase durations"),
        "description": gettext_lazy("Compare phrase files with subtitle intervals before you start assembly."),
        "key": "check-durations",
    },
    "create-subtitles": {
        "title": gettext_lazy("Create synchronized subtitles"),
        "description": gettext_lazy("Match a prepared script to a finished voiceover and export timed subtitles."),
        "key": "create-subtitles",
    },
}


def tool_prototype(request, tool_slug):
    tool = TOOL_PROTOTYPES.get(tool_slug)
    if tool is None:
        raise Http404
    form_class = TOOL_FORMS[tool_slug]
    form = form_class(request.POST or None, request.FILES or None)
    if request.method == "POST" and form.is_valid():
        job = _create_media_tool_job(request, tool_slug, form.cleaned_data)
        start_media_tool_worker(job)
        return redirect("media-tool-job", job_id=job.id)
    return render(request, "studio/tool_prototype.html", {"tool": tool, "form": form})


TOOL_FILE_ROLES = {
    "audio-to-text": {"media": "media"},
    "replace-fragment": {"source": "source", "replacement": "replacement"},
    "split-phrases": {"audio": "audio", "script": "script"},
    "fit-subtitles": {"subtitles": "subtitles", "voiceovers": "voiceover"},
    "prepare-voiceover": {"files": "voiceover"},
    "check-durations": {"subtitles": "subtitles", "phrase_files": "phrase"},
    "create-subtitles": {"script": "script", "voiceover": "voiceover"},
}


def _create_media_tool_job(request, kind, cleaned):
    roles = TOOL_FILE_ROLES[kind]
    options = {}
    uploads = []
    for key, value in cleaned.items():
        if key not in roles:
            options[key] = value
            continue
        if not value:
            continue
        values = value if isinstance(value, (list, tuple)) else [value]
        uploads.extend((roles[key], position, upload) for position, upload in enumerate(values))
    job = MediaToolJob.objects.create(
        owner=request.user if request.user.is_authenticated else None,
        kind=kind,
        options=options,
    )
    for role, position, upload in uploads:
        MediaToolInput.objects.create(job=job, role=role, position=position, media_file=upload)
    return job


def _media_tool_job_for_request(request, job_id):
    job = get_object_or_404(MediaToolJob, id=job_id)
    if job.owner_id and (not request.user.is_authenticated or job.owner_id != request.user.id):
        raise Http404
    return job


def media_tool_job(request, job_id):
    job = _media_tool_job_for_request(request, job_id)
    tool = TOOL_PROTOTYPES[job.kind]
    suffix = Path(job.result_file).suffix.lower() if job.result_file else ""
    preview_kind = "video" if suffix in {".mp4", ".webm", ".mkv", ".mov"} else "audio" if suffix in {".mp3", ".wav", ".m4a", ".ogg", ".aac", ".flac"} else ""
    return render(request, "studio/media_tool_job.html", {
        "job": job,
        "tool": tool,
        "output_url": f"{settings.MEDIA_URL}{job.result_file}" if job.result_file else "",
        "preview_kind": preview_kind,
    })


def media_tool_status(request, job_id):
    job = _media_tool_job_for_request(request, job_id)
    return JsonResponse({
        "status": job.processing_status,
        "step": job.processing_step,
        "complete": job.processing_status == "complete",
        "failed": job.processing_status == "failed",
        "error": job.processing_error,
    })


@login_required
def localization_detail(request, project_id):
    project = get_object_or_404(LocalizationProject, pk=project_id, owner=request.user)
    return render(request, "studio/localization_detail.html", {
        "localization": project,
        "versions": project.versions.select_related("project"),
        "version_form": LocalizationVersionForm(),
    })


@require_POST
@login_required
def localization_version_upload(request, project_id, version_id):
    localization = get_object_or_404(LocalizationProject, pk=project_id, owner=request.user)
    version = get_object_or_404(LocalizationVersion, pk=version_id, localization=localization)
    form = LocalizationVersionForm(request.POST, request.FILES)
    if not form.is_valid():
        messages.error(request, " ".join(error for errors in form.errors.values() for error in errors))
        return redirect("localization-detail", project_id=localization.id)
    version.script_text = form.cleaned_data["script_text"].strip()
    if form.cleaned_data.get("script_file"):
        version.script_file = form.cleaned_data["script_file"]
    version.audio_file = form.cleaned_data["audio_file"]
    version.save()
    if version.project_id:
        project = version.project
        project.audio_file.name = version.audio_file.name
        project.audio_revision = uuid.uuid4()
        project.timecodes_text = version.script_text
        project.timecodes_file.name = version.script_file.name if version.script_file else ""
        project.processing_status = "pending"
        project.pending_reprocessing = {"kind": "full", "from_position": 1, "positions": []}
        project.output_file = ""
        project.export_manifest = {}
        project.save()
    else:
        project = Project.objects.create(
            owner=request.user,
            audio_file=version.audio_file.name,
            video_file=localization.source_video.name,
            timecodes_text=version.script_text,
            timecodes_file=version.script_file.name if version.script_file else None,
            export_format=localization.export_format,
            normalize_loudness=localization.normalize_loudness,
            speed_warning_threshold=localization.speed_warning_threshold,
        )
        version.project = project
        version.save(update_fields=["project"])
    start_project_worker(
        project,
        clear_manual_boundaries=True,
        clear_replacements=True,
    )
    return redirect("localization-detail", project_id=localization.id)


@login_required
def localization_export(request, project_id):
    localization = get_object_or_404(LocalizationProject, pk=project_id, owner=request.user)
    output_dir = Path(settings.MEDIA_ROOT) / "localizations" / str(localization.id) / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / "localized-versions.zip"
    completed = [version for version in localization.versions.select_related("project") if version.project_id and version.project.output_file]
    if not completed:
        messages.error(request, _("Render at least one language version before batch export."))
        return redirect("localization-detail", project_id=localization.id)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for version in completed:
            source = Path(settings.MEDIA_ROOT) / version.project.output_file
            bundle.write(source, f"{version.language_code}/{source.name}")
            for name, relative in version.project.export_manifest.items():
                sidecar = Path(settings.MEDIA_ROOT) / relative
                if sidecar.is_file():
                    bundle.write(sidecar, f"{version.language_code}/{sidecar.name}")
    return FileResponse(archive.open("rb"), as_attachment=True, filename=archive.name)


@login_required
def takes_detail(request, project_id):
    project = get_object_or_404(TakesProject, pk=project_id, owner=request.user)
    phrases = list(project.phrases.prefetch_related("candidates", "candidates__recording"))
    return render(request, "studio/takes_detail.html", {
        "takes_project": project,
        "phrases": phrases,
        "selected_count": sum(phrase.candidates.filter(selected=True).exists() for phrase in phrases),
    })


def takes_status(request, project_id):
    project = get_object_or_404(TakesProject, pk=project_id)
    if not request.user.is_authenticated or project.owner_id != request.user.id:
        raise Http404
    return JsonResponse({"status": project.processing_status, "step": project.processing_step, "failed": project.processing_status == "failed", "error": project.processing_error})


@require_POST
@login_required
def select_take(request, project_id, candidate_id):
    project = get_object_or_404(TakesProject, pk=project_id, owner=request.user)
    candidate = get_object_or_404(TakeCandidate, pk=candidate_id, phrase__takes_project=project)
    candidate.phrase.candidates.update(selected=False)
    candidate.selected = True
    candidate.rejected = False
    candidate.save(update_fields=["selected", "rejected"])
    candidate.phrase.status = "selected"
    candidate.phrase.save(update_fields=["status"])
    return redirect("takes-detail", project_id=project.id)


@require_POST
@login_required
def update_take_boundary(request, project_id, candidate_id):
    from .services.takes import recut_take_candidate
    project = get_object_or_404(TakesProject, pk=project_id, owner=request.user)
    candidate = get_object_or_404(TakeCandidate, pk=candidate_id, phrase__takes_project=project)
    try:
        candidate.source_start = float(request.POST["source_start"])
        candidate.source_end = float(request.POST["source_end"])
        candidate.manual = True
        recut_take_candidate(candidate)
        candidate.save(update_fields=["source_start", "source_end", "manual"])
    except (KeyError, ValueError, ProcessingError) as error:
        messages.error(request, str(error))
    return redirect("takes-detail", project_id=project.id)


@require_POST
@login_required
def assemble_takes(request, project_id):
    from .services.takes import assemble_takes_project
    project = get_object_or_404(TakesProject, pk=project_id, owner=request.user)
    try:
        result = assemble_takes_project(project)
    except ProcessingError as error:
        messages.error(request, str(error))
        return redirect("takes-detail", project_id=project.id)
    return redirect("project-summary", project_id=result.id)


@login_required
def upload_audio(request):
    form = VideoUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
    )
    if request.method == "POST" and form.is_valid():
        project = form.save(commit=False)
        project.owner = request.user
        project.save()
        return redirect("project-timing", project_id=project.id)
    return render(request, "studio/upload_source_video.html", {"form": form})


@login_required
def edit_audio(request, project_id):
    project = _project_for_user(request, project_id)
    previous_audio = project.audio_file.path if project.audio_file else ""
    form = VoiceoverAudioForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        instance=project,
    )
    if request.method == "POST" and form.is_valid():
        audio_changed = "audio_file" in form.changed_data
        form.save()
        if audio_changed:
            _delete_replaced_file(
                previous_audio,
                Path(settings.MEDIA_ROOT) / "jobs" / str(project.id) / "input" / "audio",
            )
            project.audio_revision = uuid.uuid4()
            project.processing_status = "pending"
            project.processing_step = "pending"
            project.processing_error = ""
            project.output_file = ""
            project.export_manifest = {}
            project.pending_reprocessing = {"kind": "full", "from_position": 1, "positions": []}
            project.save(update_fields=[
                "processing_status", "processing_step", "processing_error",
                "audio_revision", "output_file", "export_manifest",
                "pending_reprocessing",
            ])
        return redirect("project-summary", project_id=project.id)
    return render(request, "studio/upload_audio.html", {"form": form, "project": project})


@login_required
def upload_video(request, project_id):
    project = _project_for_user(request, project_id)
    previous_video = project.video_file.path if project.video_file else ""
    form = VideoUploadForm(
        request.POST if request.method == "POST" else None,
        request.FILES if request.method == "POST" else None,
        instance=project,
    )
    if request.method == "POST" and form.is_valid():
        video_changed = "video_file" in form.changed_data
        project = form.save()
        if video_changed:
            _delete_replaced_file(
                previous_video,
                Path(settings.MEDIA_ROOT) / "jobs" / str(project.id) / "input" / "video",
            )
            project.timing_project = None
            project.timecodes_text = ""
            project.timecodes_file = None
            project.processing_status = "stale" if project.segments.exists() else "pending"
            project.processing_step = "pending"
            project.processing_error = ""
            project.output_file = ""
            project.export_manifest = {}
            project.pending_reprocessing = {
                "kind": "structure", "from_position": 1, "positions": [],
            } if project.segments.exists() else {}
            project.save(update_fields=[
                "timing_project", "timecodes_text", "timecodes_file",
                "processing_status", "processing_step", "processing_error",
                "output_file", "export_manifest", "pending_reprocessing",
            ])
            messages.success(request, _("Source video replaced. Create timecodes for the new video."))
        elif form.changed_data:
            project.output_file = ""
            project.export_manifest = {}
            update_fields = ["output_file", "export_manifest"]
            if "normalize_loudness" in form.changed_data and project.segments.exists():
                project.pending_reprocessing = merge_change_plans(
                    project.pending_reprocessing,
                    {
                        "kind": "timing", "from_position": 1,
                        "positions": list(project.segments.values_list("position", flat=True)),
                    },
                )
                project.processing_status = "stale"
                update_fields.extend(["pending_reprocessing", "processing_status"])
            project.save(update_fields=update_fields)
            messages.success(request, _("Project settings saved."))
        return redirect("project-timing", project_id=project.id)
    return render(request, "studio/upload_source_video.html", {"form": form, "project": project})


@login_required
def project_timing(request, project_id):
    project = _project_for_user(request, project_id)
    creating = request.method == "POST" and request.POST.get("timing_action") == "create"
    importing = request.method == "POST" and request.POST.get("timing_action") == "import"
    timing_form = PhraseTimingToolForm(
        request.POST if creating else None,
        request.FILES if creating else None,
        media_override=project.video_file,
    )
    import_form = ProjectTimecodesForm(
        request.POST if importing else None,
        request.FILES if importing else None,
        instance=project if importing else None,
    )
    if creating and timing_form.is_valid():
        timing = timing_form.save(commit=False)
        timing.owner = request.user
        timing.video_file = project.video_file.name
        automatic = initialize_phrase_timing_project(timing)
        project.timing_project = timing
        project.save(update_fields=["timing_project", "last_opened_at"])
        if automatic:
            start_phrase_timing_worker(timing)
        return redirect("phrase-timing-editor", project_id=timing.id)
    if importing and import_form.is_valid():
        previous_phrases = _read_project_timecodes(project)
        imported = import_form.save(commit=False)
        imported.timing_project = None
        if request.FILES.get("timecodes_file"):
            imported.timecodes_text = ""
        else:
            imported.timecodes_file = None
        imported.save()
        _record_timing_revision(imported, previous_phrases, _read_project_timecodes(imported))
        return redirect("edit-audio", project_id=project.id)
    return render(request, "studio/project_timing.html", {
        "project": project, "timing_form": timing_form, "import_form": import_form,
    })


def _read_project_timecodes(project):
    text = project.timecodes_text.strip()
    filename = ""
    if not text and project.timecodes_file:
        try:
            text = Path(project.timecodes_file.path).read_text(encoding="utf-8-sig")
            filename = project.timecodes_name
        except OSError:
            return []
    if not text:
        return []
    try:
        return parse_script(text, filename=filename)
    except SubtitleParseError:
        return []


def _processed_timing(project):
    return [
        {
            "text": segment.text,
            "start": segment.video_start,
            "end": segment.video_end,
        }
        for segment in project.segments.order_by("position")
    ]


def _saved_transcription_matches_audio(project):
    if not (
        project.transcription_file
        and project.transcription_audio_revision
        and project.transcription_audio_revision == project.audio_revision
    ):
        return False
    work_dir = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id)
    return (
        (Path(settings.MEDIA_ROOT) / project.transcription_file).is_file()
        and (work_dir / "source-44100.wav").is_file()
    )


def _start_saved_audio_update(project, plan):
    kind = (plan or {}).get("kind")
    work_dir = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id)
    if kind == "timing" and (work_dir / "alignment.json").is_file():
        return start_project_worker(
            project, timing_only_positions=plan.get("positions", []),
        )
    if kind == "structure" and _saved_transcription_matches_audio(project):
        return start_project_worker(
            project,
            reuse_transcription=True,
            realign_from_position=int(plan.get("from_position") or 1),
            preserve_following=False,
        )
    return False


def _record_timing_revision(project, previous_phrases, new_phrases):
    if previous_phrases:
        remap_project_annotations(project, previous_phrases, new_phrases)
    plan = build_change_plan(_processed_timing(project), new_phrases)
    plan = merge_change_plans(project.pending_reprocessing, plan)
    if plan.get("kind") == "text":
        segments = list(project.segments.order_by("position"))
        for segment, phrase in zip(segments, new_phrases):
            text = str(phrase.get("text", "")).strip()
            if text and segment.text != text:
                segment.text = text
                segment.save(update_fields=["text"])
        project.export_manifest = {}
        plan = {}
    project.pending_reprocessing = plan
    if plan and project.segments.exists():
        project.processing_status = "stale"
        project.output_file = ""
        project.export_manifest = {}
    project.save(update_fields=[
        "pending_reprocessing", "processing_status", "output_file",
        "export_manifest", "last_opened_at",
    ])
    if plan and project.segments.exists():
        _start_saved_audio_update(project, plan)


@login_required
def complete_project_timing(request, project_id):
    project = _project_for_user(request, project_id)
    if not project.timing_project_id:
        return redirect("project-timing", project_id=project.id)
    timing = project.timing_project
    if not timing.phrases or any(item.get("start") is None for item in timing.phrases):
        messages.error(request, _("Mark every phrase before continuing to the voiceover."))
        return redirect("phrase-timing-editor", project_id=timing.id)
    previous_phrases = _read_project_timecodes(project)
    try:
        project.timecodes_text = export_timed_phrases(
            timing.phrases, "txt", timing.video_duration, include_unmarked=False,
        )
    except ValueError as error:
        messages.error(request, str(error))
        return redirect("phrase-timing-editor", project_id=timing.id)
    project.timecodes_file = None
    project.save(update_fields=["timecodes_text", "timecodes_file", "last_opened_at"])
    _record_timing_revision(project, previous_phrases, timing.phrases)
    return redirect("edit-audio", project_id=project.id)


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
        if segment.source_duration is not None:
            player_duration = max(0.0, float(segment.source_duration))
        elif segment.audio_start is not None and segment.audio_end is not None:
            player_duration = max(0.0, float(segment.audio_end) - float(segment.audio_start))
        else:
            player_duration = None
        segment.player_duration = (
            format(player_duration, ".6f").rstrip("0").rstrip(".")
            if player_duration is not None else None
        )
    diagnostics = diagnose_segments(
        segments,
        DiagnosticOptions(speed_warning=project.speed_warning_threshold),
    )
    for segment, problems in zip(segments, diagnostics):
        segment.problems = problems
        segment.has_problems = bool(problems)
    latest_job = project.jobs.order_by("-created_at").first()
    local_project_path = ""
    local_export_path = ""
    if settings.VOICEOVER_SHOW_LOCAL_PATHS:
        local_project_path = str((Path(settings.MEDIA_ROOT) / "jobs" / str(project.id)).resolve())
        local_export_path = str((Path(local_project_path) / "output").resolve())
    return render(request, "studio/project_summary.html", {
        "project": project,
        "segments": segments,
        "output_url": output_url,
        "processing_steps": processing_step_labels(project, latest_job),
        "processing_stage_index": processing_stage_index(project, latest_job),
        "problem_count": sum(bool(problems) for problems in diagnostics),
        "latest_job": latest_job,
        "export_manifest": project.export_manifest,
        "local_project_path": local_project_path,
        "local_export_path": local_export_path,
        "can_recut_without_recognition": _saved_transcription_matches_audio(project),
    })


@login_required
def process_audio(request, project_id):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    project = _project_for_user(request, project_id)
    plan = project.pending_reprocessing or {}
    kind = plan.get("kind")
    action = request.POST.get("processing_action", "")
    saved_alignment = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id) / "alignment.json"
    can_reuse = _saved_transcription_matches_audio(project)
    can_apply_timing = saved_alignment.is_file() and project.segments.exclude(audio_file="").exists()
    options = {}
    if action in {"recut-preserve", "recut-reset"} and can_reuse:
        options.update({
            "reuse_transcription": True,
            "realign_from_position": 1,
            "preserve_following": False,
        })
        if action == "recut-reset":
            options.update({
                "clear_manual_boundaries": True,
                "clear_replacements": True,
            })
    elif kind == "timing" and can_apply_timing:
        options["timing_only_positions"] = plan.get("positions", [])
    elif kind in {"text", "structure"} and can_reuse:
        options.update({
            "reuse_transcription": True,
            "realign_from_position": int(plan.get("from_position") or 1),
            "preserve_following": kind == "text",
        })
    elif kind == "full":
        options.update({
            "clear_manual_boundaries": True,
            "clear_replacements": True,
        })
    elif project.segments.exists() and not kind:
        messages.info(request, _("There are no changes to apply. The existing fragments were kept."))
        return redirect("project-summary", project_id=project.id)
    if start_project_worker(project, **options):
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
    elif project.pending_reprocessing or project.processing_status == "stale":
        messages.error(request, _("Apply the phrase changes before building the final result."))
    elif start_project_render_worker(project):
        messages.info(request, _("Final rendering started. You may leave this page and return later."))
    else:
        messages.info(request, _("Processing is already running."))
    return redirect("project-summary", project_id=project.id)


@login_required
def processing_status(request, project_id):
    project = _project_for_user(request, project_id)
    project = _recover_stale_processing(project)
    latest_job = project.jobs.order_by("-created_at").first()
    return JsonResponse({
        "status": project.processing_status,
        "stage_index": processing_stage_index(project, latest_job),
        "complete": project.processing_status == "complete",
        "failed": project.processing_status in {"failed", "cancelled"},
        "error": project.processing_error,
        "progress": latest_job.progress if latest_job else 0,
        "job_status": latest_job.status if latest_job else "",
        "cancel_requested": latest_job.cancel_requested if latest_job else False,
    })


@require_POST
@login_required
def cancel_processing(request, project_id):
    project = _project_for_user(request, project_id)
    job = project.jobs.filter(status__in={"queued", "running"}).order_by("-created_at").first()
    if job:
        job.cancel_requested = True
        if job.status == "queued":
            job.status = "cancelled"
            job.finished_at = timezone.now()
            project.processing_status = "cancelled"
            project.processing_error = _("Processing was cancelled.")
            project.save(update_fields=["processing_status", "processing_error"])
            release_storage(f"voiceover:{project.pk}")
        job.save(update_fields=["cancel_requested", "status", "finished_at"])
    return redirect("project-summary", project_id=project.id)


@require_POST
@login_required
def retry_processing(request, project_id):
    project = _project_for_user(request, project_id)
    previous = project.jobs.filter(status__in={"failed", "cancelled"}).order_by("-created_at").first()
    if not previous:
        messages.error(request, _("There is no failed job to retry."))
    elif previous.kind == ProcessingJob.KIND_RENDER:
        start_project_render_worker(project)
    else:
        start_project_worker(
            project,
            reuse_transcription=bool(previous.payload.get("reuse_transcription")),
            realign_from_position=previous.payload.get("realign_from_position"),
            preserve_following=bool(previous.payload.get("preserve_following", True)),
            timing_only_positions=previous.payload.get("timing_only_positions"),
        )
    return redirect("project-summary", project_id=project.id)


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
    if Path(uploaded.name).suffix.lower() not in ALLOWED_AUDIO:
        messages.error(request, _("Unsupported audio format."))
        return redirect("project-summary", project_id=project.id)
    replacement = project.segment_replacements.filter(position=position).first()
    if replacement:
        previous_audio = replacement.audio_file.path
        replacement.audio_file = uploaded
        replacement.audio_start = 0
        replacement.audio_end = None
        replacement.save()
        _delete_replaced_file(
            previous_audio,
            Path(settings.MEDIA_ROOT) / "jobs" / str(project.id) / "replacements" / str(position) / "input",
        )
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


@require_POST
@login_required
def update_segment_text(request, project_id, position):
    project = _project_for_user(request, project_id)
    if project.processing_status in {"queued", "processing"}:
        return JsonResponse({"error": _("Processing is already running.")}, status=409)
    segment = get_object_or_404(Segment, project=project, position=position)
    try:
        payload = json.loads(request.body or "{}")
    except json.JSONDecodeError:
        return JsonResponse({"error": _("Invalid editor data.")}, status=400)
    text = str(payload.get("text", "")).strip()
    if not text:
        return JsonResponse({"error": _("Phrase text cannot be empty.")}, status=400)
    if text == segment.text:
        return JsonResponse({"ok": True, "text": text})

    segment.text = text
    segment.save(update_fields=["text"])

    if project.timing_project_id:
        timing = project.timing_project
        phrases = [dict(item) for item in timing.phrases]
        if position <= len(phrases):
            phrases[position - 1]["text"] = text
            phrases[position - 1]["words"] = []
            phrases[position - 1]["issues"] = [
                issue for issue in phrases[position - 1].get("issues", [])
                if issue != "low_confidence"
            ]
            timing.phrases = phrases
            timing.source_text = "\n".join(item["text"] for item in phrases)
            timing.save(update_fields=["phrases", "source_text", "updated_at"])
            project.timecodes_text = export_timed_phrases(
                phrases, "txt", timing.video_duration, include_unmarked=False,
            )
    else:
        phrases = [
            {"text": item.text, "start": item.video_start, "end": item.video_end}
            for item in project.segments.order_by("position")
        ]
        project.timecodes_text = export_timed_phrases(phrases, "txt", include_unmarked=False)
    project.timecodes_file = None
    project.export_manifest = {}
    project.save(update_fields=[
        "timecodes_text", "timecodes_file", "export_manifest", "last_opened_at",
    ])
    return JsonResponse({"ok": True, "text": text})


@require_GET
@login_required
def download_project_export(request, project_id, export_name):
    project = _project_for_user(request, project_id)
    relative = project.export_manifest.get(export_name)
    if not relative:
        return JsonResponse({"error": _("Export file is not available.")}, status=404)
    media_root = Path(settings.MEDIA_ROOT).resolve()
    path = (media_root / relative).resolve()
    project_output = (media_root / "jobs" / str(project.id) / "output").resolve()
    if project_output not in path.parents or not path.is_file():
        return JsonResponse({"error": _("Export file is not available.")}, status=404)
    return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)


@require_GET
@login_required
def download_project_clips(request, project_id):
    project = _project_for_user(request, project_id)
    clips = {
        segment.position: Path(segment.audio_file.path)
        for segment in project.segments.exclude(audio_file="")
    }
    if not clips:
        return JsonResponse({"error": _("Export file is not available.")}, status=404)
    output_dir = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id) / "output"
    archive = output_dir / "voiceover-clips.zip"
    reservation_key = f"clips:{project.pk}"
    if not archive.is_file():
        reserve_storage(reservation_key, sum(path.stat().st_size for path in clips.values() if path.is_file()))
        try:
            archive = create_clips_archive(output_dir, clips)
        finally:
            release_storage(reservation_key)
    return FileResponse(archive.open("rb"), as_attachment=True, filename=archive.name)



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


def _range_file_response(request, path, content_type):
    size = path.stat().st_size
    range_header = request.headers.get("Range", "")
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", range_header.strip())
    if not match:
        response = FileResponse(path.open("rb"), content_type=content_type)
        response["Content-Length"] = size
        response["Accept-Ranges"] = "bytes"
        response["Content-Disposition"] = f'inline; filename="{path.name}"'
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
        response["Accept-Ranges"] = "bytes"
        return response
    end = min(end, size - 1)
    length = end - start + 1
    response = StreamingHttpResponse(
        _file_range(path, start, length),
        status=206,
        content_type=content_type,
    )
    response["Content-Length"] = length
    response["Content-Range"] = f"bytes {start}-{end}/{size}"
    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = f'inline; filename="{path.name}"'
    return response


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
    return _range_file_response(request, path, "audio/wav")


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
    projects = Project.objects.filter(
        owner=request.user,
        localization_version__isnull=True,
        takes_source__isnull=True,
    ).order_by("-created_at")
    return render(request, "studio/dashboard.html", {
        "projects": projects,
        "localizations": LocalizationProject.objects.filter(owner=request.user).prefetch_related("versions").order_by("-created_at"),
        "takes_projects": TakesProject.objects.filter(owner=request.user).prefetch_related("phrases").order_by("-created_at"),
    })


@login_required
def delete_project(request, project_id):
    project = _project_for_user(request, project_id)
    project = _recover_stale_processing(project)
    if request.method == "POST":
        if project.processing_status in {"queued", "processing"}:
            messages.error(request, _("Wait until project processing is complete before deleting it."))
            return redirect("dashboard")
        project_directory = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id)
        release_storage(f"voiceover:{project.pk}")
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
