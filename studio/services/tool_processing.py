import csv
import io
import json
import re
import shutil
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from groq import Groq

from studio.models import MediaToolJob
from studio.services.voiceover import ProcessingError, load_groq_key, probe_duration, run, transcribe
from voiceover_core.alignment import align_entries, tokenize
from voiceover_core.media import atempo_filters, video_codec_arguments
from voiceover_core.subtitles import (
    SubtitleParseError,
    format_srt,
    format_vtt,
    parse_manual_ranges,
    parse_plain_phrases,
    parse_script,
)


def _update(job, step):
    job.processing_step = step
    MediaToolJob.objects.filter(pk=job.pk).update(processing_step=step)


def _input(job, role, position=0):
    item = job.inputs.filter(role=role, position=position).first()
    if not item:
        raise ProcessingError(f"Missing input: {role}")
    return Path(item.media_file.path)


def _inputs(job, role):
    return [Path(item.media_file.path) for item in job.inputs.filter(role=role).order_by("position")]


def _output_dir(job):
    directory = Path(settings.MEDIA_ROOT) / "tools" / "jobs" / str(job.id) / "output"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _relative(path):
    return Path(path).relative_to(settings.MEDIA_ROOT).as_posix()


def _read_script(path, allow_plain=False):
    text = Path(path).read_text(encoding="utf-8-sig")
    try:
        return parse_script(text, Path(path).name)
    except SubtitleParseError:
        if allow_plain:
            return parse_plain_phrases(text)
        raise


def _archive(directory, name, paths):
    archive = directory / name
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in paths:
            bundle.write(path, Path(path).name)
    return archive


def build_docx_bytes(text):
    """Create a small standards-compliant DOCX without a heavyweight dependency."""
    paragraphs = []
    for paragraph in re.split(r"\n\s*\n", str(text).strip()):
        lines = paragraph.splitlines() or [""]
        runs = "<w:br/>".join(
            f'<w:r><w:t xml:space="preserve">{escape(line)}</w:t></w:r>'
            for line in lines
        )
        paragraphs.append(f"<w:p>{runs}</w:p>")
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body>{''.join(paragraphs)}<w:sectPr/></w:body></w:document>"
    )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/word/document.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
        '</Types>'
    )
    relationships = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
        'Target="word/document.xml"/></Relationships>'
    )
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", relationships)
        archive.writestr("word/document.xml", document)
    return output.getvalue()


def _paragraphize_transcription(payload, divide_paragraphs=True):
    segments = payload.get("segments") or []
    if not segments:
        return re.sub(r"\s+", " ", payload.get("text", "")).strip()
    if not divide_paragraphs:
        return " ".join(re.sub(r"\s+", " ", str(item.get("text", ""))).strip() for item in segments).strip()
    paragraphs, current, previous_end = [], [], None
    for segment in segments:
        text = re.sub(r"\s+", " ", str(segment.get("text", ""))).strip()
        if not text:
            continue
        start = float(segment.get("start", previous_end or 0))
        pause = start - previous_end if previous_end is not None else 0
        if current and (pause >= 1.35 or sum(map(len, current)) >= 650):
            paragraphs.append(" ".join(current))
            current = []
        current.append(text)
        previous_end = float(segment.get("end", start))
    if current:
        paragraphs.append(" ".join(current))
    return "\n\n".join(paragraphs)


def _word_signature(text):
    return re.findall(r"[^\W_]+(?:['’][^\W_]+)?", str(text).casefold(), flags=re.UNICODE)


def _format_readable_text(text, preserve_wording=True):
    client = Groq(api_key=load_groq_key())
    instruction = (
        "Add accurate punctuation, capitalization and meaningful paragraph breaks to this transcript. "
        "Do not summarize, explain, translate, censor or add information. "
        "Preserve every spoken word and its order. Return only the formatted transcript."
    )
    chunks, current = [], []
    for paragraph in re.split(r"\n\s*\n", text):
        if current and sum(map(len, current)) + len(paragraph) > 12000:
            chunks.append("\n\n".join(current)); current = []
        if len(paragraph) <= 12000:
            current.append(paragraph)
            continue
        words, piece = paragraph.split(), []
        for word in words:
            if piece and sum(map(len, piece)) + len(piece) + len(word) > 12000:
                chunks.append(" ".join(piece)); piece = []
            piece.append(word)
        if piece:
            chunks.append(" ".join(piece))
    if current:
        chunks.append("\n\n".join(current))
    formatted_parts = []
    for chunk in chunks:
        completion = client.chat.completions.create(
            model=settings.GROQ_TEXT_MODEL,
            messages=[{"role": "system", "content": instruction}, {"role": "user", "content": chunk}],
            temperature=0,
        )
        formatted_parts.append((completion.choices[0].message.content or "").strip())
    formatted = "\n\n".join(part for part in formatted_parts if part).strip()
    if not formatted:
        raise ProcessingError("The text formatting model returned an empty result")
    if preserve_wording and _word_signature(formatted) != _word_signature(text):
        raise ProcessingError("Formatting was rejected because the model changed recognized words")
    return formatted


def _transcribe_prepared_audio(job, prepared, output, language, prompt):
    transcript_json = output / "transcription.json"
    duration = probe_duration(prepared)
    if duration <= 900:
        return transcribe(prepared, transcript_json, language=language, prompt=prompt)
    chunks_dir = output / "recognition-chunks"
    chunks_dir.mkdir(parents=True, exist_ok=True)
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(prepared),
        "-map", "0:a:0", "-c", "copy", "-f", "segment", "-segment_time", "900",
        "-reset_timestamps", "1", str(chunks_dir / "%03d.flac"),
    ])
    paths = sorted(chunks_dir.glob("*.flac"))
    combined = {"text": "", "words": [], "segments": [], "duration": duration, "language": language or ""}
    offset = 0.0
    for index, path in enumerate(paths, 1):
        _update(job, f"recognizing speech {index}/{len(paths)}")
        part = transcribe(path, chunks_dir / f"{path.stem}.json", language=language, prompt=prompt)
        if not combined["language"]:
            combined["language"] = part.get("language", "")
        combined["text"] = f"{combined['text']} {part.get('text', '')}".strip()
        for word in part.get("words") or []:
            combined["words"].append(dict(word, start=float(word["start"]) + offset, end=float(word["end"]) + offset))
        for segment in part.get("segments") or []:
            item = dict(segment)
            item["start"] = float(item.get("start", 0)) + offset
            item["end"] = float(item.get("end", 0)) + offset
            combined["segments"].append(item)
        offset += probe_duration(path)
    transcript_json.write_text(json.dumps(combined, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return combined


def _transcribe_audio(job):
    source = _input(job, "media")
    output = _output_dir(job)
    prepared = output / "recognition.flac"
    _update(job, "preparing audio")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-map", "0:a:0", "-ar", "16000", "-ac", "1", "-c:a", "flac", str(prepared),
    ])
    _update(job, "recognizing speech")
    payload = _transcribe_prepared_audio(
        job, prepared, output,
        job.options.get("language") or None,
        job.options.get("terms") or None,
    )
    prepared.unlink(missing_ok=True)
    shutil.rmtree(output / "recognition-chunks", ignore_errors=True)
    text = _paragraphize_transcription(payload, job.options.get("divide_paragraphs", True))
    if not text:
        raise ProcessingError("No speech was recognized in the selected file")
    formatting_applied, warning = False, ""
    if job.options.get("transcription_mode") == "readable":
        _update(job, "formatting text")
        try:
            text = _format_readable_text(text, job.options.get("preserve_wording", True))
            formatting_applied = True
        except Exception as error:
            warning = str(error)
    txt = output / "transcript.txt"
    docx = output / "transcript.docx"
    txt.write_text(text.rstrip() + "\n", encoding="utf-8")
    docx.write_bytes(build_docx_bytes(text))
    export_format = job.options.get("export_format", "txt")
    if export_format == "docx":
        result = docx
    elif export_format == "both":
        result = _archive(output, "transcript-txt-docx.zip", [txt, docx])
    else:
        result = txt
    job.result_manifest = {
        "text": text,
        "formatting_applied": formatting_applied,
        "warning": warning,
        "txt": _relative(txt),
        "docx": _relative(docx),
        "transcription": _relative(output / "transcription.json"),
        "language": payload.get("language") or job.options.get("language") or "",
        "duration": payload.get("duration"),
    }
    return result


def _audio_filters(duration, speed=1.0, normalize=False, fade=.008, volume=1.0):
    filters = []
    filters.extend(atempo_filters(speed))
    if volume != 1:
        filters.append(f"volume={volume:.4f}")
    if normalize:
        filters.append("loudnorm=I=-16:LRA=7:TP=-1.5")
    filters.extend([
        f"afade=t=in:st=0:d={min(fade, duration / 2):.4f}",
        f"afade=t=out:st={max(0, duration - min(fade, duration / 2)):.6f}:d={min(fade, duration / 2):.4f}",
    ])
    return filters


def _replace_fragment(job):
    source, replacement = _input(job, "source"), _input(job, "replacement")
    start, end = float(job.options["start"]), float(job.options["end"])
    available = end - start
    source_duration, replacement_duration = probe_duration(source), probe_duration(replacement)
    if end > source_duration + .01:
        raise ProcessingError("The selected interval is outside the source file")
    speed = max(1.0, replacement_duration / available) if job.options.get("keep_duration", True) else 1.0
    rendered_duration = replacement_duration / speed
    fade = int(job.options.get("transition_ms", 40)) / 1000
    volume = float(job.options.get("volume", 1))
    out = _output_dir(job)
    probe = run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name", "-of", "json", str(source)])
    streams = json.loads(probe.stdout).get("streams", [])
    has_video = any(stream.get("codec_type") == "video" for stream in streams)
    video_codec = next((stream.get("codec_name") for stream in streams if stream.get("codec_type") == "video"), "")
    target = out / ("updated-video.mp4" if has_video else "updated-audio.m4a")
    replacement_filters = ["asetpts=PTS-STARTPTS", *_audio_filters(rendered_duration, speed=speed, fade=fade, volume=volume)]
    if job.options.get("keep_duration", True):
        replacement_filters.extend(["apad", f"atrim=duration={available:.6f}"])
    delay = round(start * 1000)
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-i", str(replacement)]
    command.extend([
        "-filter_complex",
        f"[0:a]volume=enable='between(t,{start:.6f},{end:.6f})':volume=0[base];"
        f"[1:a]{','.join(replacement_filters)},adelay={delay}:all=1[new];"
        "[base][new]amix=inputs=2:duration=first:normalize=0,alimiter=limit=0.95[aout]",
    ])
    if has_video:
        command.extend(["-map", "0:v:0", "-map", "[aout]", *video_codec_arguments(video_codec)])
    else:
        command.extend(["-map", "[aout]"])
    command.extend(["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(target)])
    run(command)
    job.result_manifest = {"speed": round(speed, 3), "source_duration": source_duration, "replacement_duration": replacement_duration}
    return target


def _detect_sound_ranges(path, minimum_pause):
    # Silence metadata is written to stderr even when FFmpeg succeeds, so capture it
    # directly instead of using the regular command wrapper.
    import subprocess
    completed = subprocess.run([
        "ffmpeg", "-hide_banner", "-i", str(path), "-af",
        f"silencedetect=noise=-42dB:d={minimum_pause}", "-f", "null", "-",
    ], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode:
        raise ProcessingError(completed.stderr.strip().splitlines()[-1])
    duration = probe_duration(path)
    starts = [float(value) for value in re.findall(r"silence_start:\s*([0-9.]+)", completed.stderr)]
    ends = [float(value) for value in re.findall(r"silence_end:\s*([0-9.]+)", completed.stderr)]
    silences = list(zip(starts, ends))
    ranges, cursor = [], 0.0
    for silence_start, silence_end in silences:
        if silence_start - cursor >= .08:
            ranges.append((cursor, silence_start))
        cursor = max(cursor, silence_end)
    if duration - cursor >= .08:
        ranges.append((cursor, duration))
    return ranges


def _split_phrases(job):
    audio = _input(job, "audio")
    method = job.options.get("method", "script")
    if method == "script":
        entries = _read_script(_input(job, "script"))
        ranges = [(entry["video_start"], entry["video_end"], entry["text"]) for entry in entries]
    elif method == "manual":
        entries = parse_manual_ranges(job.options.get("manual_boundaries", ""))
        ranges = [(entry["video_start"], entry["video_end"], entry["text"]) for entry in entries]
    else:
        ranges = [(start, end, "") for start, end in _detect_sound_ranges(audio, float(job.options.get("silence_seconds", .8)))]
    out, clips = _output_dir(job), []
    extension = job.options.get("output_format", "wav")
    for index, (start, end, text) in enumerate(ranges, 1):
        slug = re.sub(r"[^\w-]+", "_", text, flags=re.UNICODE).strip("_")[:48]
        target = out / f"{index:03d}{'_' + slug if slug else ''}.{extension}"
        codec = ["-c:a", "pcm_s24le"] if extension == "wav" else ["-c:a", "libmp3lame", "-b:a", "192k"]
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.6f}", "-t", f"{end-start:.6f}", "-i", str(audio), "-vn", *codec, str(target)])
        clips.append(target)
    job.result_manifest = {"count": len(clips)}
    return _archive(out, "voiceover-phrases.zip", clips)


def _render_timed_audio(entries, sources, output, normalize=True):
    duration = max(entry["video_end"] for entry in entries)
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-t", f"{duration:.6f}", "-i", "anullsrc=r=48000:cl=stereo"]
    for source in sources:
        command.extend(["-i", str(source)])
    filters, labels = ["[0:a]anull[base]"], ["[base]"]
    for index, (entry, source) in enumerate(zip(entries, sources), 1):
        source_duration = probe_duration(source)
        available = entry["video_end"] - entry["video_start"]
        speed = max(1.0, source_duration / max(.05, available))
        expected = min(source_duration / speed, available)
        parts = atempo_filters(speed)
        if normalize:
            parts.append("loudnorm=I=-16:LRA=7:TP=-1.5")
        parts.extend(["afade=t=in:st=0:d=0.008", f"afade=t=out:st={max(0, expected-0.008):.6f}:d=0.008", f"atrim=duration={available:.6f}", f"adelay={round(entry['video_start']*1000)}:all=1"])
        filters.append(f"[{index}:a]{','.join(parts)}[a{index}]")
        labels.append(f"[a{index}]")
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0[aout]")
    command.extend(["-filter_complex", ";".join(filters), "-map", "[aout]", "-c:a", "pcm_s24le", "-ar", "48000", str(output)])
    run(command)


def _fit_subtitles(job):
    entries = _read_script(_input(job, "subtitles"))
    voiceovers = _inputs(job, "voiceover")
    out, prepared = _output_dir(job), []
    if len(voiceovers) == 1 and len(entries) > 1:
        source = voiceovers[0]
        flac, transcript = out / "recognition.flac", out / "transcription.json"
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "flac", str(flac)])
        words = transcribe(flac, transcript)["words"]
        flac.unlink(missing_ok=True)
        aligned_entries = [dict(entry, tokens=tokenize(entry["text"])) for entry in entries]
        align_entries(aligned_entries, words)
        for entry in aligned_entries:
            if entry.get("audio_start") is None:
                raise ProcessingError(f"Phrase {entry['id']} could not be found in the recording")
            clip = out / f"source-{entry['id']:03d}.wav"
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{entry['audio_start']:.6f}", "-t", f"{entry['audio_end']-entry['audio_start']:.6f}", "-i", str(source), "-vn", "-c:a", "pcm_s16le", str(clip)])
            prepared.append(clip)
    else:
        if len(voiceovers) != len(entries):
            raise ProcessingError("The number of phrase files must match the number of subtitle intervals")
        prepared = voiceovers
    report, limit = [], float(job.options.get("speed_limit", 1.12))
    for entry, source in zip(entries, prepared):
        audio_duration = probe_duration(source)
        available = entry["video_end"] - entry["video_start"]
        speed = max(1.0, audio_duration / max(.05, available))
        report.append({"position": entry["id"], "text": entry["text"], "available": round(available, 3), "audio": round(audio_duration, 3), "speed": round(speed, 3), "status": "review" if speed > limit else "ok"})
    target = out / "fitted-voiceover.wav"
    _render_timed_audio(entries, prepared, target)
    report_path = out / "duration-report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    job.result_manifest = {"report": report, "report_file": _relative(report_path)}
    return target


def _prepare_voiceover(job):
    out, results = _output_dir(job), []
    extension = job.options.get("output_format", "wav")
    for index, source in enumerate(_inputs(job, "voiceover"), 1):
        target = out / f"{index:03d}-{source.stem}.{extension}"
        filters = []
        if job.options.get("trim_edges"):
            trim = "silenceremove=start_periods=1:start_duration=0.03:start_threshold=-50dB"
            filters.extend([trim, "areverse", trim, "areverse"])
        if job.options.get("normalize"):
            target_lufs = float(job.options.get("loudness_target", -16))
            filters.append(f"loudnorm=I={target_lufs}:LRA=7:TP=-1.5")
        if job.options.get("prevent_clipping"):
            filters.append("alimiter=limit=0.95")
        if job.options.get("soft_edges"):
            duration = probe_duration(source)
            filters.extend(["afade=t=in:st=0:d=0.008", f"afade=t=out:st={max(0, duration-0.008):.6f}:d=0.008"])
        codec = ["-c:a", "pcm_s24le"] if extension == "wav" else ["-c:a", "libmp3lame", "-b:a", "192k"]
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vn"]
        if filters:
            command.extend(["-af", ",".join(filters)])
        command.extend([*codec, str(target)])
        run(command)
        results.append(target)
    job.result_manifest = {"count": len(results)}
    return results[0] if len(results) == 1 else _archive(out, "prepared-voiceover.zip", results)


def _check_durations(job):
    entries, files = _read_script(_input(job, "subtitles")), _inputs(job, "phrase")
    threshold = float(job.options.get("warning_threshold", 1.12))
    rows = []
    for index, entry in enumerate(entries):
        available = entry["video_end"] - entry["video_start"]
        audio_duration = probe_duration(files[index]) if index < len(files) else None
        speed = max(1.0, audio_duration / max(.05, available)) if audio_duration is not None else None
        status = "missing" if audio_duration is None else ("rewrite" if speed > 1.5 else "review" if speed > threshold else "ok")
        rows.append({"position": entry["id"], "text": entry["text"], "available": round(available, 3), "audio": round(audio_duration, 3) if audio_duration is not None else None, "speed": round(speed, 3) if speed else None, "status": status})
    out = _output_dir(job)
    report = out / "duration-report.csv"
    with report.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["position", "text", "available", "audio", "speed", "status"])
        writer.writeheader(); writer.writerows(rows)
    job.result_manifest = {"report": rows}
    return report


def _create_subtitles(job):
    script_path, audio = _input(job, "script"), _input(job, "voiceover")
    phrases = _read_script(script_path, allow_plain=True)
    out, flac, transcript = _output_dir(job), None, None
    flac, transcript = out / "recognition.flac", out / "transcription.json"
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(audio), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "flac", str(flac)])
    words = transcribe(flac, transcript)["words"]
    flac.unlink(missing_ok=True)
    aligned = align_entries([dict(item, tokens=tokenize(item["text"])) for item in phrases], words)
    missing = [item["id"] for item in aligned if item.get("audio_start") is None]
    timed = [{"video_start": item["audio_start"], "video_end": item["audio_end"], "text": item["text"]} for item in aligned if item.get("audio_start") is not None]
    if not timed:
        raise ProcessingError("No script phrases could be matched with the recording")
    extension = job.options.get("subtitle_format", "srt")
    target = out / f"voiceover-subtitles.{extension}"
    target.write_text(format_vtt(timed) if extension == "vtt" else format_srt(timed), encoding="utf-8")
    job.result_manifest = {"matched": len(timed), "missing": missing}
    return target


PROCESSORS = {
    MediaToolJob.KIND_TRANSCRIBE: _transcribe_audio,
    MediaToolJob.KIND_REPLACE: _replace_fragment,
    MediaToolJob.KIND_SPLIT: _split_phrases,
    MediaToolJob.KIND_FIT: _fit_subtitles,
    MediaToolJob.KIND_PREPARE: _prepare_voiceover,
    MediaToolJob.KIND_DURATIONS: _check_durations,
    MediaToolJob.KIND_SUBTITLES: _create_subtitles,
}


def process_media_tool_job(job):
    job.processing_status, job.processing_step, job.processing_error = "processing", "preparing", ""
    job.save(update_fields=["processing_status", "processing_step", "processing_error"])
    try:
        processor = PROCESSORS.get(job.kind)
        if not processor:
            raise ProcessingError("Unknown media tool")
        _update(job, "processing")
        result = processor(job)
        _update(job, "finalizing")
        job.result_file = _relative(result)
        job.processing_status = "complete"
        job.processing_step = "complete"
        job.finished_at = timezone.now()
        job.save(update_fields=["result_file", "result_manifest", "processing_status", "processing_step", "finished_at"])
    except Exception as error:
        job.processing_status = "failed"
        job.processing_error = str(error)
        job.finished_at = timezone.now()
        job.save(update_fields=["processing_status", "processing_error", "finished_at"])
        raise
