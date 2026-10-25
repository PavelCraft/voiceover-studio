"""Automatic phrase timing with Groq word timestamps and local Silero VAD."""

from __future__ import annotations

import json
import math
import re
import shutil
import wave
from pathlib import Path

import numpy as np
import onnxruntime
from django.conf import settings

from studio.models import PhraseTimingProject
from studio.services.voiceover import ProcessingError, probe_duration, probe_has_audio, run, transcribe

SAMPLE_RATE = 16_000
WINDOW = 512
TERMINAL = re.compile(r"[.!?…][\"'»)]*$")
SOFT_END = re.compile(r"[,;:][\"'»)]*$")


def _update(project, step, status="processing", error=""):
    PhraseTimingProject.objects.filter(pk=project.pk).update(
        processing_status=status, processing_step=step, processing_error=error,
    )


def _word_text(words):
    raw = [str(item.get("raw", item.get("text", item.get("word", "")))) for item in words]
    if any(value[:1].isspace() for value in raw):
        return "".join(raw).strip()
    text = " ".join(value.strip() for value in raw if value.strip())
    return re.sub(r"\s+([,.;:!?…])", r"\1", text).strip()


def _segment_for_word(word, segments):
    midpoint = (float(word["start"]) + float(word["end"])) / 2
    return next((item for item in segments if float(item.get("start", 0)) <= midpoint <= float(item.get("end", 0))), {})


def build_phrases(payload):
    """Turn Groq words into readable phrases while retaining every word anchor."""
    segments = payload.get("segments") or []
    words = []
    for source in payload.get("words") or []:
        text = str(source.get("word", "")).strip()
        if not text:
            continue
        segment = _segment_for_word(source, segments)
        words.append({
            "text": text,
            "raw": str(source.get("word", text)),
            "start": round(float(source["start"]), 3),
            "end": round(float(source["end"]), 3),
            "uncertain": float(segment.get("avg_logprob", 0)) < -0.8 or float(segment.get("no_speech_prob", 0)) > 0.6,
        })
    phrases, current = [], []
    for index, word in enumerate(words):
        current.append(word)
        next_word = words[index + 1] if index + 1 < len(words) else None
        gap = (next_word["start"] - word["end"]) if next_word else math.inf
        duration = word["end"] - current[0]["start"]
        terminal = bool(TERMINAL.search(word["text"]))
        should_break = terminal or gap >= .65 or len(current) >= 28 or duration >= 12
        if duration >= 8 and SOFT_END.search(word["text"]):
            should_break = True
        if should_break:
            phrases.append(current)
            current = []
    if current:
        phrases.append(current)

    # Avoid isolated filler-sized rows created by punctuation or brief gaps.
    merged = []
    for group in phrases:
        duration = group[-1]["end"] - group[0]["start"]
        if merged and len(group) <= 2 and duration < .8:
            merged[-1].extend(group)
        else:
            merged.append(group)
    result = [{
        "id": index,
        "text": _word_text(group),
        "start": group[0]["start"],
        "end": group[-1]["end"],
        "end_explicit": True,
        "words": group,
        "issues": (["low_confidence"] if any(item["uncertain"] for item in group) else []),
        "source": "automatic",
    } for index, group in enumerate(merged, 1)]
    for index, phrase in enumerate(result[:-1]):
        phrase["end"] = min(phrase["end"], result[index + 1]["start"])
    return result


def clamp_phrases_to_duration(phrases, duration):
    """Keep recognition rounding from placing explicit ends past the media."""
    duration = max(0.0, float(duration))
    result = []
    for source in phrases:
        start = float(source.get("start", 0))
        if start >= duration:
            continue
        phrase = dict(source)
        phrase["end"] = round(min(float(source.get("end", duration)), duration), 3)
        phrase["words"] = [
            {
                **word,
                "end": round(min(float(word.get("end", duration)), duration), 3),
            }
            for word in source.get("words", [])
            if float(word.get("start", 0)) < duration
        ]
        result.append(phrase)
    return result


def _transcribe_long(project, prepared, directory):
    duration = probe_duration(prepared)
    if duration <= 900:
        return transcribe(prepared, directory / "transcription.json")
    chunks = directory / "recognition-chunks"
    chunks.mkdir(parents=True, exist_ok=True)
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(prepared),
         "-map", "0:a:0", "-c", "copy", "-f", "segment", "-segment_time", "900",
         "-reset_timestamps", "1", str(chunks / "%03d.flac")])
    paths = sorted(chunks.glob("*.flac"))
    combined = {"text": "", "words": [], "segments": [], "duration": duration, "language": ""}
    offset = 0.0
    for number, path in enumerate(paths, 1):
        _update(project, f"recognizing {number}/{len(paths)}")
        part = transcribe(path, chunks / f"{path.stem}.json")
        combined["text"] = f"{combined['text']} {part.get('text', '')}".strip()
        combined["language"] = combined["language"] or part.get("language", "")
        for key in ("words", "segments"):
            for source in part.get(key) or []:
                item = dict(source)
                item["start"] = float(item.get("start", 0)) + offset
                item["end"] = float(item.get("end", 0)) + offset
                combined[key].append(item)
        offset += probe_duration(path)
    (directory / "transcription.json").write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")
    return combined


def speech_probabilities(wav_path):
    model = settings.BASE_DIR / "voiceover_core" / "models" / "silero_vad.onnx"
    options = onnxruntime.SessionOptions()
    options.inter_op_num_threads = options.intra_op_num_threads = 1
    session = onnxruntime.InferenceSession(str(model), providers=["CPUExecutionProvider"], sess_options=options)
    state = np.zeros((2, 1, 128), dtype=np.float32)
    context = np.zeros((1, 64), dtype=np.float32)
    probabilities, total = [], 0
    with wave.open(str(wav_path), "rb") as audio:
        if audio.getframerate() != SAMPLE_RATE or audio.getnchannels() != 1 or audio.getsampwidth() != 2:
            raise ProcessingError("Silero VAD requires mono 16 kHz PCM audio")
        while data := audio.readframes(WINDOW):
            samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
            total += len(samples)
            if len(samples) < WINDOW:
                samples = np.pad(samples, (0, WINDOW - len(samples)))
            current = samples.reshape(1, -1)
            model_input = np.concatenate((context, current), axis=1)
            output, state = session.run(None, {"input": model_input, "state": state, "sr": np.array(SAMPLE_RATE, dtype=np.int64)})
            probabilities.append(float(output[0][0]))
            context = model_input[:, -64:]
    return probabilities, total


def speech_intervals(probabilities, total_samples, threshold=.5, min_speech=.25, min_silence=.2, pad=.03):
    """Convert Silero frame probabilities to conservative speech intervals."""
    negative = max(.01, threshold - .15)
    started = None
    silence = None
    ranges = []
    for index, probability in enumerate(probabilities):
        sample = index * WINDOW
        if started is None and probability >= threshold:
            started, silence = sample, None
        elif started is not None and probability < negative:
            silence = sample if silence is None else silence
            if sample - silence >= min_silence * SAMPLE_RATE:
                if silence - started >= min_speech * SAMPLE_RATE:
                    ranges.append((max(0, started / SAMPLE_RATE - pad), min(total_samples / SAMPLE_RATE, silence / SAMPLE_RATE + pad)))
                started = silence = None
        elif started is not None and probability >= threshold:
            silence = None
    if started is not None and total_samples - started >= min_speech * SAMPLE_RATE:
        ranges.append((max(0, started / SAMPLE_RATE - pad), total_samples / SAMPLE_RATE))
    return ranges


def _subtract(interval, covers):
    remaining = [interval]
    for cover_start, cover_end in covers:
        next_ranges = []
        for start, end in remaining:
            if cover_end <= start or cover_start >= end:
                next_ranges.append((start, end))
            else:
                if cover_start > start:
                    next_ranges.append((start, cover_start))
                if cover_end < end:
                    next_ranges.append((cover_end, end))
        remaining = next_ranges
    return remaining


def uncovered_speech_intervals(speech, words, minimum=.55):
    covers = sorted((max(0, float(word["start"]) - .18), float(word["end"]) + .18) for word in words)
    missing = [part for interval in speech for part in _subtract(interval, covers) if part[1] - part[0] >= minimum]
    merged = []
    for start, end in missing:
        if merged and start - merged[-1][1] <= .12:
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return [{"id": f"unrecognized-{index}", "kind": "unrecognized_speech", "start": round(start, 3),
             "end": round(end, 3), "resolved": False} for index, (start, end) in enumerate(merged, 1)]


def process_phrase_timing_project(project):
    directory = Path(settings.MEDIA_ROOT) / "tools" / "phrase-timing" / str(project.id) / "automatic"
    directory.mkdir(parents=True, exist_ok=True)
    flac, wav = directory / "recognition.flac", directory / "vad.wav"
    try:
        _update(project, "preparing")
        if not probe_has_audio(project.video_file.path):
            raise ProcessingError("The uploaded file has no audio track. Automatic timing requires audio with speech.")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", project.video_file.path,
             "-map", "0:a:0", "-ar", "16000", "-ac", "1", "-c:a", "flac", str(flac)])
        _update(project, "recognizing")
        payload = _transcribe_long(project, flac, directory)
        phrases = build_phrases(payload)
        if not phrases:
            raise ProcessingError("No speech was recognized in the uploaded file")
        _update(project, "checking_voice")
        run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", project.video_file.path,
             "-map", "0:a:0", "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(wav)])
        probabilities, samples = speech_probabilities(wav)
        speech = speech_intervals(probabilities, samples)
        review = uncovered_speech_intervals(speech, payload.get("words") or [])
        duration = probe_duration(Path(project.video_file.path))
        phrases = clamp_phrases_to_duration(phrases, duration)
        PhraseTimingProject.objects.filter(pk=project.pk).update(
            phrases=phrases, source_text="\n".join(item["text"] for item in phrases),
            review_items=review, video_duration=duration, processing_status="complete",
            processing_step="complete", processing_error="",
        )
    except Exception as error:
        _update(project, "failed", status="failed", error=str(error))
        raise
    finally:
        for path in (flac, wav):
            if path.exists():
                path.unlink()
        shutil.rmtree(directory / "recognition-chunks", ignore_errors=True)

