import csv
import io
import json
import math
import re
import shutil
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from django.conf import settings
from django.utils import timezone
from groq import Groq

from studio.models import MediaToolJob
from studio.services.voiceover import (
    ProcessingError, _write_waveform_peaks, detect_silences, load_groq_key, probe_duration, run,
    transcribe_long_audio,
)
from voiceover_core.alignment import align_entries, snap_boundaries, tokenize
from voiceover_core.media import (
    MAX_AUDIO_EDIT_SPEED, MIN_AUDIO_EDIT_SPEED,
    atempo_filters, mp3_codec_arguments, normalize_mp3_bitrate, video_codec_arguments,
)
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
    return transcribe_long_audio(
        prepared, output / "transcription.json", language=language, prompt=prompt,
        progress=lambda index, total: _update(job, f"recognizing speech {index}/{total}"),
    )


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


def _split_phrase_lines(text):
    phrases = [re.sub(r"\s+", " ", line).strip() for line in str(text).lstrip("\ufeff").splitlines()]
    phrases = [phrase for phrase in phrases if phrase]
    if not phrases:
        raise ProcessingError("The text contains no phrases")
    return phrases


def _split_phrase_target(output, segment, extension):
    slug = re.sub(r"[^\w-]+", "_", segment["text"], flags=re.UNICODE).strip("_")[:48]
    return output / f"{int(segment['position']):03d}{'_' + slug if slug else ''}.{extension}"


def render_split_phrase(job, segment):
    manifest = job.result_manifest or {}
    source = Path(settings.MEDIA_ROOT) / manifest["source"]
    output = _output_dir(job)
    extension = manifest.get("output_format") or job.options.get("output_format", "wav")
    mp3_bitrate = normalize_mp3_bitrate(
        manifest.get("mp3_bitrate") or job.options.get("mp3_bitrate")
    )
    old_file = Path(settings.MEDIA_ROOT) / segment["file"] if segment.get("file") else None
    target = _split_phrase_target(output, segment, extension)
    if old_file and old_file != target:
        old_file.unlink(missing_ok=True)
    start, end = float(segment["audio_start"]), float(segment["audio_end"])
    codec = ["-c:a", "pcm_s24le"] if extension == "wav" else mp3_codec_arguments(mp3_bitrate)
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.6f}", "-t", f"{end - start:.6f}", "-i", str(source),
        "-vn", *codec, str(target),
    ])
    segment["file"] = _relative(target)
    segment["duration"] = round(probe_duration(target), 6)
    return target


def rebuild_split_phrases_archive(job):
    manifest = job.result_manifest or {}
    clips = [Path(settings.MEDIA_ROOT) / item["file"] for item in manifest.get("segments", []) if item.get("file")]
    archive = _archive(_output_dir(job), "voiceover-phrases.zip", clips)
    manifest["archive"] = _relative(archive)
    manifest["count"] = len(clips)
    job.result_manifest = manifest
    return archive


def _split_phrases(job):
    audio, output = _input(job, "audio"), _output_dir(job)
    text = job.options.get("script_text", "")
    if not text and job.inputs.filter(role="script").exists():
        text = _input(job, "script").read_text(encoding="utf-8-sig")
    phrases = _split_phrase_lines(text)
    source_wav, recognition = output / "source-44100.wav", output / "recognition.flac"
    preview, peaks_path = output / "editor-preview.mp3", output / "waveform-peaks.json"
    _update(job, "preparing audio")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(audio), "-vn", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(source_wav)])
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(audio), "-vn", "-ar", "16000", "-ac", "1", "-c:a", "flac", str(recognition)])
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source_wav), "-vn", "-c:a", "libmp3lame", "-b:a", "128k", str(preview)])
    duration = _write_waveform_peaks(source_wav, peaks_path)
    _update(job, "recognizing speech")
    transcript_path = output / "transcription.json"
    payload = transcribe_long_audio(
        recognition, transcript_path,
        progress=lambda index, total: _update(job, f"recognizing speech {index}/{total}"),
    )
    recognition.unlink(missing_ok=True)
    entries = [{"id": index, "text": phrase, "tokens": tokenize(phrase)} for index, phrase in enumerate(phrases, 1)]
    _update(job, "matching phrases")
    entries = align_entries(entries, payload["words"])
    _update(job, "trimming edge pauses")
    entries = snap_boundaries(entries, detect_silences(source_wav))
    segments = []
    for entry in entries:
        segment = {
            "position": entry["id"], "text": entry["text"],
            "audio_start": entry.get("audio_start"), "audio_end": entry.get("audio_end"),
            "score": entry.get("score"), "status": entry.get("status", "not_found"),
            "boundary_status": entry.get("boundary_status", "word"), "manual": False, "file": "",
        }
        segments.append(segment)
    job.result_manifest = {
        "stage": "review", "source": _relative(source_wav), "preview": _relative(preview),
        "peaks": _relative(peaks_path), "transcription": _relative(transcript_path),
        "duration": duration, "output_format": job.options.get("output_format", "wav"),
        "mp3_bitrate": normalize_mp3_bitrate(job.options.get("mp3_bitrate")),
        "segments": segments,
    }
    _update(job, "cutting phrases")
    for segment in segments:
        if segment["audio_start"] is not None and segment["audio_end"] is not None:
            render_split_phrase(job, segment)
    archive = rebuild_split_phrases_archive(job)
    return archive


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
        words = transcribe_long_audio(flac, transcript)["words"]
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
    mp3_bitrate = normalize_mp3_bitrate(job.options.get("mp3_bitrate"))
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
        codec = ["-c:a", "pcm_s24le"] if extension == "wav" else mp3_codec_arguments(mp3_bitrate)
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source), "-vn"]
        if filters:
            command.extend(["-af", ",".join(filters)])
        command.extend([*codec, str(target)])
        run(command)
        results.append(target)
    job.result_manifest = {"count": len(results), "mp3_bitrate": mp3_bitrate}
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
    words = transcribe_long_audio(flac, transcript)["words"]
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


def _prepare_audio_editor(job):
    output = _output_dir(job)
    existing = {int(item["input_id"]): item for item in (job.result_manifest or {}).get("tracks", []) if item.get("input_id")}
    items = list(job.inputs.filter(role="audio").order_by("position", "id"))
    by_id = {item.id: item for item in items}
    requested = [int(value) for value in job.options.get("editor_track_order", []) if str(value).isdigit()]
    ordered_ids = [value for value in requested if value in by_id]
    ordered_ids.extend(item.id for item in items if item.id not in ordered_ids)
    tracks = []
    for number, input_id in enumerate(ordered_ids, 1):
        item = by_id[input_id]
        cached = existing.get(input_id, {})
        cached_preview = Path(settings.MEDIA_ROOT) / cached.get("preview", "") if cached.get("preview") else None
        cached_peaks = Path(settings.MEDIA_ROOT) / cached.get("peaks", "") if cached.get("peaks") else None
        if cached_preview and cached_peaks and cached_preview.is_file() and cached_peaks.is_file():
            tracks.append(cached)
            continue
        source = Path(item.media_file.path)
        wav = output / f"track-{input_id}-waveform.wav"
        preview = output / f"track-{input_id}-preview.mp3"
        peaks = output / f"track-{input_id}-peaks.json"
        _update(job, f"preparing waveform {number}/{len(ordered_ids)}")
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
            "-map", "0:a:0", "-vn", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(wav),
        ])
        duration = _write_waveform_peaks(wav, peaks)
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(wav),
            "-vn", "-c:a", "libmp3lame", "-b:a", "128k", str(preview),
        ])
        wav.unlink(missing_ok=True)
        stored_name = Path(item.media_file.name).name
        prefix = f"{item.position:03d}-"
        display_name = stored_name[len(prefix):] if stored_name.startswith(prefix) else stored_name
        tracks.append({
            "input_id": input_id, "name": display_name, "duration": round(float(duration), 6),
            "preview": _relative(preview), "peaks": _relative(peaks),
        })
    if not tracks:
        raise ProcessingError("Missing input: audio")
    first = tracks[0]
    job.result_manifest = {
        "stage": "editing",
        "editor_ready": True,
        "duration": round(sum(float(item["duration"]) for item in tracks), 6),
        "preview": first["preview"],
        "peaks": first["peaks"],
        "tracks": tracks,
    }
    return Path(settings.MEDIA_ROOT) / first["preview"]


def _ordered_audio_editor_inputs(job):
    items = list(job.inputs.filter(role="audio").order_by("position", "id"))
    by_id = {item.id: item for item in items}
    requested = [int(value) for value in job.options.get("editor_track_order", []) if str(value).isdigit()]
    ordered = [by_id[value] for value in requested if value in by_id]
    ordered.extend(item for item in items if item.id not in {value.id for value in ordered})
    return ordered


def _validated_audio_operations(raw_operations, duration):
    if not isinstance(raw_operations, list) or len(raw_operations) > 500:
        raise ProcessingError("Invalid audio edit operations")
    operations = []
    for raw in raw_operations:
        if not isinstance(raw, dict) or raw.get("type") not in {"cut", "speed", "silence"}:
            raise ProcessingError("Invalid audio edit operation")
        try:
            start = float(raw["start"])
            end = float(raw["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise ProcessingError("Invalid audio edit boundaries") from error
        source_length = end - start
        inserted_silence = raw["type"] == "silence" and abs(source_length) < 0.001
        if (
            not all(math.isfinite(value) for value in (start, end))
            or start < 0
            or end > duration + 0.01
            or source_length < -0.001
            or (source_length < 0.001 and not inserted_silence)
        ):
            raise ProcessingError("Invalid audio edit boundaries")
        item = {"type": raw["type"], "start": max(0.0, start), "end": min(duration, end)}
        if item["type"] == "speed":
            try:
                speed = float(raw["speed"])
            except (KeyError, TypeError, ValueError) as error:
                raise ProcessingError("Invalid audio speed") from error
            if not math.isfinite(speed) or not MIN_AUDIO_EDIT_SPEED <= speed <= MAX_AUDIO_EDIT_SPEED:
                raise ProcessingError("Invalid audio speed")
            item["speed"] = speed
        elif item["type"] == "silence":
            try:
                silence_duration = float(raw["duration"])
            except (KeyError, TypeError, ValueError) as error:
                raise ProcessingError("Invalid silence duration") from error
            if not math.isfinite(silence_duration) or silence_duration <= 0:
                raise ProcessingError("Invalid silence duration")
            item["duration"] = silence_duration
        operations.append(item)
    operations.sort(key=lambda item: (item["start"], item["end"]))
    for previous, current in zip(operations, operations[1:]):
        if current["start"] < previous["end"] - 0.001:
            raise ProcessingError("Audio edit intervals must not overlap")
    return operations


def _audio_piece_filter(index, start, end, speed=1.0):
    source_duration = end - start
    output_duration = source_duration / speed
    filters = [f"atrim=start={start:.6f}:end={end:.6f}", "asetpts=PTS-STARTPTS", *atempo_filters(speed)]
    fade = min(0.005, output_duration / 4)
    if fade > 0:
        filters.extend([f"afade=t=in:st=0:d={fade:.6f}", f"afade=t=out:st={max(0, output_duration-fade):.6f}:d={fade:.6f}"])
    return f"[0:a:0]{','.join(filters)}[p{index}]", f"[p{index}]"


def _audio_silence_filter(index, duration):
    return (
        f"anullsrc=r=44100:cl=stereo,atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[p{index}]",
        f"[p{index}]",
    )


def _render_audio_editor(job):
    inputs = _ordered_audio_editor_inputs(job)
    if not inputs:
        raise ProcessingError("Missing input: audio")
    output = _output_dir(job)
    temporary_source = None
    if len(inputs) == 1:
        source = Path(inputs[0].media_file.path)
    else:
        temporary_source = output / "combined-source.wav"
        command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        for item in inputs:
            command.extend(["-i", str(item.media_file.path)])
        normalized = [
            f"[{index}:a:0]aresample=44100,aformat=sample_fmts=s16:channel_layouts=stereo,asetpts=PTS-STARTPTS[a{index}]"
            for index in range(len(inputs))
        ]
        concat = "".join(f"[a{index}]" for index in range(len(inputs))) + f"concat=n={len(inputs)}:v=0:a=1[out]"
        command.extend(["-filter_complex", ";".join([*normalized, concat]), "-map", "[out]", "-c:a", "pcm_s16le", str(temporary_source)])
        _update(job, "joining source audio")
        run(command)
        source = temporary_source
    track_durations = [float(item.get("duration", 0)) for item in (job.result_manifest or {}).get("tracks", [])]
    duration = sum(track_durations) if track_durations and all(value > 0 for value in track_durations) else probe_duration(source)
    operations = _validated_audio_operations(job.options.get("operations", []), duration)
    output_format = job.options.get("output_format", "mp3")
    if output_format not in {"mp3", "wav"}:
        raise ProcessingError("Unsupported audio editor output format")
    target = output / f"edited-audio.{output_format}"
    mp3_bitrate = normalize_mp3_bitrate(job.options.get("mp3_bitrate"))
    filters, labels, cursor, piece_index, rendered_duration = [], [], 0.0, 0, 0.0

    def add_piece(start, end, speed=1.0):
        nonlocal piece_index, rendered_duration
        if end - start < 0.001:
            return
        expression, label = _audio_piece_filter(piece_index, start, end, speed)
        filters.append(expression)
        labels.append(label)
        piece_index += 1
        rendered_duration += (end - start) / speed

    def add_silence(silence_duration):
        nonlocal piece_index, rendered_duration
        expression, label = _audio_silence_filter(piece_index, silence_duration)
        filters.append(expression)
        labels.append(label)
        piece_index += 1
        rendered_duration += silence_duration

    for operation in operations:
        add_piece(cursor, operation["start"])
        if operation["type"] == "speed":
            add_piece(operation["start"], operation["end"], operation["speed"])
        elif operation["type"] == "silence":
            add_silence(operation["duration"])
        cursor = operation["end"]
    add_piece(cursor, duration)
    trailing_silence = float(job.options.get("trailing_silence", 0) or 0)
    if not math.isfinite(trailing_silence) or trailing_silence < 0:
        raise ProcessingError("Invalid trailing silence duration")
    if trailing_silence:
        add_silence(trailing_silence)
    if not labels:
        raise ProcessingError("The edits remove the complete audio track")
    if len(labels) == 1:
        filter_complex = f"{filters[0]};{labels[0]}anull[out]"
    else:
        filter_complex = f"{';'.join(filters)};{''.join(labels)}concat=n={len(labels)}:v=0:a=1[out]"
    codec = ["-c:a", "pcm_s24le"] if output_format == "wav" else mp3_codec_arguments(mp3_bitrate)
    _update(job, "rendering edits")
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(source),
        "-filter_complex", filter_complex, "-map", "[out]", "-vn", *codec, str(target),
    ])
    if temporary_source:
        temporary_source.unlink(missing_ok=True)
    manifest = dict(job.result_manifest or {})
    manifest.update({
        "stage": "result",
        "editor_ready": True,
        "operations": operations,
        # MP3 containers include encoder delay/padding. Keep the user-facing
        # duration in the same decoded-audio timeline used by the editor.
        "output_duration": round(rendered_duration, 6),
        "output_format": output_format,
        "mp3_bitrate": mp3_bitrate,
    })
    job.result_manifest = manifest
    return target


def _edit_audio(job):
    if job.options.get("phase") == "render":
        return _render_audio_editor(job)
    return _prepare_audio_editor(job)


PROCESSORS = {
    MediaToolJob.KIND_TRANSCRIBE: _transcribe_audio,
    MediaToolJob.KIND_EDIT_AUDIO: _edit_audio,
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
