import json
from pathlib import Path

from django.conf import settings

from studio.models import Project, Segment, TakeCandidate, TakePhrase, TakesProject
from studio.services.voiceover import (
    MIN_VIDEO_GAP,
    ProcessingError,
    fitting_filters,
    probe_duration,
    render_project_result,
    run,
    transcribe,
)
from voiceover_core.alignment import find_take_candidates, tokenize
from voiceover_core.subtitles import parse_script


def _script(project, video_duration=None):
    text = project.script_text.strip()
    name = ""
    if not text and project.script_file:
        text = Path(project.script_file.path).read_text(encoding="utf-8-sig")
        name = Path(project.script_file.name).name
    return parse_script(text, name, video_duration)


def _project_dir(project):
    directory = Path(settings.MEDIA_ROOT) / "takes" / str(project.id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def process_takes_project(project):
    project.processing_status, project.processing_step, project.processing_error = "processing", "transcribing", ""
    project.save(update_fields=["processing_status", "processing_step", "processing_error"])
    root = _project_dir(project)
    try:
        entries = _script(project, probe_duration(project.video_file.path))
        project.phrases.all().delete()
        phrases = {
            entry["id"]: TakePhrase.objects.create(
                takes_project=project,
                position=entry["id"],
                text=entry["text"],
                video_start=entry["video_start"],
                video_end=entry["video_end"],
            )
            for entry in entries
        }
        prepared_entries = [dict(entry, tokens=tokenize(entry["text"])) for entry in entries]
        total = 0
        for recording in project.recordings.all():
            recording_dir = root / "recordings" / f"{recording.position:03d}"
            recording_dir.mkdir(parents=True, exist_ok=True)
            source_wav, source_flac = recording_dir / "source.wav", recording_dir / "recognition.flac"
            transcription_path = recording_dir / "transcription.json"
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", recording.media_file.path, "-vn", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(source_wav)])
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", recording.media_file.path, "-vn", "-ar", "16000", "-ac", "1", "-c:a", "flac", str(source_flac)])
            words = transcribe(source_flac, transcription_path)["words"]
            recording.transcription_file = transcription_path.relative_to(settings.MEDIA_ROOT).as_posix()
            recording.save(update_fields=["transcription_file"])
            matches = find_take_candidates(prepared_entries, words, minimum_score=.43)
            for position, candidates in matches.items():
                for index, candidate in enumerate(candidates, 1):
                    clip_dir = root / "candidates" / f"{position:03d}"
                    clip_dir.mkdir(parents=True, exist_ok=True)
                    clip = clip_dir / f"r{recording.position:02d}-{index:02d}.wav"
                    start, end = candidate["audio_start"], candidate["audio_end"]
                    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.6f}", "-t", f"{end-start:.6f}", "-i", str(source_wav), "-c:a", "pcm_s16le", str(clip)])
                    TakeCandidate.objects.create(
                        phrase=phrases[position], recording=recording,
                        source_start=start, source_end=end, score=candidate["score"],
                        audio_file=clip.relative_to(settings.MEDIA_ROOT).as_posix(),
                    )
                    total += 1
        for phrase in phrases.values():
            count = phrase.candidates.count()
            phrase.status = "ready" if count else "not_found"
            phrase.save(update_fields=["status"])
        if not total:
            raise ProcessingError("No takes could be matched with the script")
        project.processing_status, project.processing_step = "complete", "selection"
        project.save(update_fields=["processing_status", "processing_step"])
    except Exception as error:
        project.processing_status, project.processing_error = "failed", str(error)
        project.save(update_fields=["processing_status", "processing_error"])
        raise


def recut_take_candidate(candidate):
    recording_source = _project_dir(candidate.phrase.takes_project) / "recordings" / f"{candidate.recording.position:03d}" / "source.wav"
    target = Path(candidate.audio_file.path)
    if candidate.source_end <= candidate.source_start:
        raise ProcessingError("The selected take boundaries are invalid")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{candidate.source_start:.6f}", "-t", f"{candidate.source_end-candidate.source_start:.6f}", "-i", str(recording_source), "-c:a", "pcm_s16le", str(target)])


def assemble_takes_project(takes_project):
    phrases = list(takes_project.phrases.prefetch_related("candidates"))
    missing = [phrase.position for phrase in phrases if not phrase.candidates.filter(selected=True).exists()]
    if missing:
        raise ProcessingError("Choose a take for phrases: " + ", ".join(map(str, missing)))
    project = takes_project.assembled_project
    if not project:
        project = Project.objects.create(
            owner=takes_project.owner,
            audio_file=takes_project.recordings.first().media_file.name,
            video_file=takes_project.video_file.name,
            timecodes_text=takes_project.script_text,
            timecodes_file=takes_project.script_file.name if takes_project.script_file else None,
            output_mode=Project.OUTPUT_VIDEO,
            processing_status="processing",
            processing_step="cutting",
        )
        takes_project.assembled_project = project
        takes_project.save(update_fields=["assembled_project"])
    project.segments.all().delete()
    work_dir = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id)
    fitted_dir = work_dir / "fitted"
    fitted_dir.mkdir(parents=True, exist_ok=True)
    alignment = []
    for phrase in phrases:
        candidate = phrase.candidates.get(selected=True)
        source = Path(candidate.audio_file.path)
        source_duration = probe_duration(source)
        available = max(.05, phrase.video_end - phrase.video_start - MIN_VIDEO_GAP)
        speed = max(1.0, source_duration / available)
        expected = min(source_duration / speed, available)
        fitted = fitted_dir / f"{phrase.position:03d}.wav"
        filters = fitting_filters(project, speed, expected, available)
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-af", ",".join(filters), "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(fitted)])
        Segment.objects.create(
            project=project, position=phrase.position, text=phrase.text,
            video_start=phrase.video_start, video_end=phrase.video_end,
            audio_start=candidate.source_start, audio_end=candidate.source_end,
            recognition_score=candidate.score, status="take",
            audio_file=candidate.audio_file.name, source_duration=source_duration,
            output_duration=probe_duration(fitted), speed=speed,
        )
        alignment.append({
            "id": phrase.position, "text": phrase.text,
            "video_start": phrase.video_start, "video_end": phrase.video_end,
            "audio_start": candidate.source_start, "audio_end": candidate.source_end,
            "status": "take", "score": candidate.score,
        })
    work_dir.mkdir(parents=True, exist_ok=True)
    (work_dir / "alignment.json").write_text(json.dumps(alignment, ensure_ascii=False, indent=2), encoding="utf-8")
    project.processing_status = "complete"
    project.processing_step = "complete"
    project.save(update_fields=["processing_status", "processing_step"])
    render_project_result(project)
    return project
