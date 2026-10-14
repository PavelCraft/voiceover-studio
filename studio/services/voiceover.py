import json
import os
import re
import subprocess
import sys
import wave
from array import array
from difflib import SequenceMatcher
from pathlib import Path

from django.conf import settings
from groq import Groq

from studio.models import Segment
from studio.services.alignment_numbers import expand_numeric_replacements

NOISE_DB = -38
MIN_SILENCE = 0.08
SEARCH_RADIUS = 0.9
MIN_VIDEO_GAP = 0.5
MIN_CURSOR_SCORE = 0.35
MIN_LOOKAHEAD_SCORE = 0.55
MIN_SNAPPED_DURATION = 0.15
NUMBER_MAP = {
    "ноль": "0", "один": "1", "два": "2", "три": "3", "четыре": "4",
    "пять": "5", "шесть": "6", "семь": "7", "восемь": "8", "девять": "9",
    "десять": "10", "zero": "0", "one": "1", "two": "2", "three": "3",
    "four": "4", "five": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "ten": "10",
    "null": "0", "eins": "1", "ein": "1", "eine": "1", "zwei": "2",
    "drei": "3", "vier": "4", "fünf": "5", "funf": "5", "sechs": "6",
    "sieben": "7", "acht": "8", "neun": "9", "zehn": "10",
    "cero": "0", "uno": "1", "una": "1", "dos": "2", "tres": "3",
    "cuatro": "4", "cinco": "5", "seis": "6", "siete": "7", "ocho": "8",
    "nueve": "9", "diez": "10", "zéro": "0", "un": "1", "une": "1",
    "deux": "2", "trois": "3", "quatre": "4", "cinq": "5", "sept": "7",
    "huit": "8", "neuf": "9", "dix": "10", "um": "1", "uma": "1",
    "dois": "2", "duas": "2", "três": "3", "tres": "3", "quatro": "4",
    "sete": "7", "oito": "8", "nove": "9", "dez": "10", "零": "0",
    "一": "1", "二": "2", "三": "3", "四": "4", "五": "5", "六": "6",
    "七": "7", "八": "8", "九": "9", "十": "10",
}


class ProcessingError(RuntimeError):
    pass


def update_progress(project, step):
    project.processing_step = step
    project.__class__.objects.filter(pk=project.pk).update(processing_step=step)


def run(command):
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        lines = result.stderr.strip().splitlines()
        raise ProcessingError(lines[-1] if lines else "FFmpeg command failed")
    return result


def probe_duration(path):
    result = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=noprint_wrappers=1:nokey=1", str(path)])
    return float(result.stdout.strip())


def _write_waveform_peaks(wav_path, peaks_path):
    peaks = []
    with wave.open(str(wav_path), "rb") as audio:
        rate = audio.getframerate()
        channels = audio.getnchannels()
        frames_per_peak = max(1, rate // 100)
        while data := audio.readframes(frames_per_peak):
            samples = array("h")
            samples.frombytes(data)
            if sys.byteorder == "big":
                samples.byteswap()
            peak = max((abs(sample) for sample in samples), default=0)
            peaks.append(round(min(1.0, peak / 32768), 4))
        duration = audio.getnframes() / rate
    peaks_path.write_text(
        json.dumps({
            "duration": duration,
            "peaks_per_second": 100,
            "channels": channels,
            "peaks": peaks,
        }),
        encoding="utf-8",
    )
    return duration


def ensure_editor_assets(project, source_wav=None):
    work_dir = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id)
    work_dir.mkdir(parents=True, exist_ok=True)
    source_wav = Path(source_wav or work_dir / "source-44100.wav")
    if not source_wav.exists():
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", project.audio_file.path, "-vn", "-ar", "44100",
            "-ac", "2", "-c:a", "pcm_s16le", str(source_wav),
        ])
    preview_path = work_dir / "editor-preview.mp3"
    peaks_path = work_dir / "waveform-peaks.json"
    source_mtime = source_wav.stat().st_mtime
    if not preview_path.exists() or preview_path.stat().st_mtime < source_mtime:
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source_wav), "-vn", "-c:a", "libmp3lame",
            "-b:a", "128k", str(preview_path),
        ])
    if not peaks_path.exists() or peaks_path.stat().st_mtime < source_mtime:
        duration = _write_waveform_peaks(source_wav, peaks_path)
    else:
        duration = json.loads(peaks_path.read_text(encoding="utf-8"))["duration"]
    return {
        "source": source_wav.relative_to(settings.MEDIA_ROOT).as_posix(),
        "audio": preview_path.relative_to(settings.MEDIA_ROOT).as_posix(),
        "peaks": peaks_path.relative_to(settings.MEDIA_ROOT).as_posix(),
        "duration": float(duration),
    }



def ensure_replacement_assets(project, replacement):
    work_dir = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id) / "replacements" / str(replacement.position)
    work_dir.mkdir(parents=True, exist_ok=True)
    source_wav = work_dir / "source-44100.wav"
    peaks_path = work_dir / "waveform-peaks.json"
    input_mtime = Path(replacement.audio_file.path).stat().st_mtime
    if not source_wav.exists() or source_wav.stat().st_mtime < input_mtime:
        run([
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", replacement.audio_file.path, "-vn", "-ar", "44100",
            "-ac", "2", "-c:a", "pcm_s16le", str(source_wav),
        ])
    source_mtime = source_wav.stat().st_mtime
    if not peaks_path.exists() or peaks_path.stat().st_mtime < source_mtime:
        duration = _write_waveform_peaks(source_wav, peaks_path)
    else:
        duration = json.loads(peaks_path.read_text(encoding="utf-8"))["duration"]
    return {
        "source": source_wav.relative_to(settings.MEDIA_ROOT).as_posix(),
        "peaks": peaks_path.relative_to(settings.MEDIA_ROOT).as_posix(),
        "duration": float(duration),

    }

def load_groq_key():
    if key := os.environ.get("GROQ_API_KEY"):
        return key
    env_path = settings.BASE_DIR / ".env"
    if env_path.exists():
        for raw_line in env_path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, value = line.split("=", 1)
            if name.strip() in {"GROQ_API_KEY", "API-KEY"} and (key := value.strip().strip("\"'")):
                return key
    raise ProcessingError("GROQ_API_KEY is not configured in .env")


def normalize_word(value):
    value = re.sub(r"[^\w+]", "", str(value).lower().replace("ё", "е"), flags=re.UNICODE)
    return NUMBER_MAP.get(value, value)


def tokenize(text):
    result = []
    buffer = []

    def flush_buffer():
        if buffer:
            if word := normalize_word("".join(buffer)):
                result.append(word)
            buffer.clear()

    for character in str(text).lower():
        if re.fullmatch(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]", character):
            flush_buffer()
            result.append(normalize_word(character))
        elif character.isalnum() or character in "_+":
            buffer.append(character)
        else:
            flush_buffer()
    flush_buffer()
    return result


def expand_transcription_words(words):
    expanded = []
    for word in words:
        tokens = tokenize(word.get("word", ""))
        if not tokens:
            continue
        start = float(word["start"])
        end = float(word["end"])
        duration = max(0.0, end - start)
        for index, token in enumerate(tokens):
            item = dict(word)
            item["word"] = token
            item["start"] = start + duration * index / len(tokens)
            item["end"] = start + duration * (index + 1) / len(tokens)
            expanded.append(item)
    return expanded


def is_cjk_alignment(target):
    if not target:
        return False
    cjk_tokens = sum(
        bool(re.fullmatch(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af]", token))
        for token in target
    )
    return cjk_tokens >= 2 and cjk_tokens >= len(target) * 0.25

def parse_timecodes(text, video_duration):
    entries = []
    current = None
    pattern = re.compile(r"^(\d+):(\d{2}(?:[.,]\d+)?)(?::(\d{2}(?:[.,]\d+)?))?\s*(?:[-–—]\s*)?(.*)$")
    for line_number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        match = pattern.match(line)
        if match:
            if current:
                entries.append(current)
            first, second, third, text = match.groups()
            second, third = second.replace(",", "."), third.replace(",", ".") if third else None
            seconds = int(first) * 60 + float(second) if third is None else int(first) * 3600 + int(float(second)) * 60 + float(third)
            current = {"video_start": float(seconds), "text": text.strip(), "line": line_number}
        elif current:
            current["text"] = f"{current['text']} {line}".strip()
        else:
            raise ProcessingError(f"Text before the first timecode on line {line_number}")
    if current:
        entries.append(current)
    if not entries:
        raise ProcessingError("The timecode file contains no phrases")
    for index, entry in enumerate(entries):
        if not entry["text"]:
            raise ProcessingError(f"No phrase after the timecode on line {entry['line']}")
        if index and entry["video_start"] <= entries[index - 1]["video_start"]:
            raise ProcessingError("Video timecodes must be strictly increasing")
        entry.update(id=index + 1, tokens=tokenize(entry["text"]))
        entry["video_end"] = entries[index + 1]["video_start"] if index + 1 < len(entries) else video_duration
    return entries

def validate_word_timestamps(words):
    latest_start = float("-inf")
    for index, word in enumerate(words):
        try:
            start = float(word["start"])
            end = float(word["end"])
        except (KeyError, TypeError, ValueError) as error:
            raise ProcessingError(f"Invalid Groq word timestamps at index {index}") from error
        if end < start:
            raise ProcessingError(f"Groq word ends before it starts at index {index}")
        if start < latest_start - 1.0:
            raise ProcessingError("Groq returned non-monotonic word timestamps")
        latest_start = max(latest_start, start)



def transcribe(audio_path, output_path):
    client = Groq(api_key=load_groq_key())
    with audio_path.open("rb") as audio:
        result = client.audio.transcriptions.create(
            file=audio, model="whisper-large-v3", response_format="verbose_json",
            timestamp_granularities=["word", "segment"], temperature=0,
        )
    payload = result.model_dump()
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    words = payload.get("words") or []
    if not words:
        raise ProcessingError("Groq returned no word-level timestamps")
    validate_word_timestamps(words)
    return payload


def _match_score(target, asr_tokens, start, end):
    return SequenceMatcher(
        None,
        target,
        asr_tokens[start:end],
        autojunk=False,
    ).ratio()


def _find_best_cjk_match(target, asr_tokens, cursor, search_end, minimum, maximum):
    """Find a CJK phrase without testing every possible start/length pair."""
    window = asr_tokens[cursor:search_end]
    matcher = SequenceMatcher(None, target, window, autojunk=False)
    blocks = [block for block in matcher.get_matching_blocks() if block.size]

    candidate_starts = {cursor}
    for block in sorted(blocks, key=lambda item: item.size, reverse=True)[:16]:
        estimated_start = cursor + block.b - block.a
        for offset in range(-2, 3):
            candidate_starts.add(estimated_start + offset)

    candidate_starts = sorted(
        start
        for start in candidate_starts
        if cursor <= start < search_end and start + minimum <= search_end
    )
    if not candidate_starts:
        return None

    margin = max(8, int(len(target) * 0.3))
    preferred_minimum = max(minimum, len(target) - margin)
    preferred_maximum = min(maximum, len(target) + margin)
    lengths = set(range(preferred_minimum, preferred_maximum + 1))
    lengths.update((minimum, maximum))

    best = None
    for start in candidate_starts:
        for length in lengths:
            end = start + length
            if end > search_end:
                continue
            score = _match_score(target, asr_tokens, start, end)
            if best is None or score > best[0]:
                best = (score, start, end)
    return best


def find_best_match(target, asr_tokens, cursor):
    search_end = min(len(asr_tokens), cursor + max(250, len(target) * 4))
    minimum = max(1, int(len(target) * 0.65))
    maximum = min(
        search_end - cursor,
        max(len(target) * 2, len(target) + 30),
    )
    if maximum < minimum:
        return None
    if is_cjk_alignment(target):
        return _find_best_cjk_match(
            target,
            asr_tokens,
            cursor,
            search_end,
            minimum,
            maximum,
        )
    best = None
    for start in range(cursor, search_end):
        for length in range(minimum, maximum + 1):
            end = start + length
            if end > search_end:
                break
            score = _match_score(target, asr_tokens, start, end)
            if best is None or score > best[0]:
                best = (score, start, end)
    return best


def align_entries(entries, words, overrides=None, fixed_entries=None):
    words = expand_numeric_replacements(entries, words, normalize_word)
    words = expand_transcription_words(words)
    asr_tokens = [normalize_word(word.get("word", "")) for word in words]
    cursor = 0
    overrides = overrides or {}
    fixed_entries = fixed_entries or {}
    for index, entry in enumerate(entries):
        fixed = fixed_entries.get(entry.get("id", index + 1))
        if fixed:
            for key in ("status", "audio_start", "audio_end", "score", "boundary_status"):
                if key in fixed:
                    entry[key] = fixed[key]
            entry["_locked"] = True
            if entry.get("audio_end") is not None:
                cursor = len(words)
                for word_index, word in enumerate(words):
                    if float(word["start"]) >= float(entry["audio_end"]) - 0.02:
                        cursor = word_index
                        break
            continue
        override = overrides.get(entry.get("id", index + 1))
        if override:
            if override.skipped:
                entry.update(
                    status="skipped",
                    audio_start=None,
                    audio_end=None,
                    score=None,
                    boundary_status="manual",
                )
                continue
            entry.update(
                status="manual",
                audio_start=float(override.audio_start),
                audio_end=float(override.audio_end),
                score=1.0,
                boundary_status="manual",
            )
            cursor = len(words)
            for word_index, word in enumerate(words):
                if float(word["start"]) >= entry["audio_end"] - 0.02:
                    cursor = word_index
                    break
            continue
        target = entry["tokens"]
        best = find_best_match(target, asr_tokens, cursor)
        if not best:
            entry.update(status="not_found", audio_start=None, audio_end=None, score=0)
            continue
        score, start, end = best
        if index + 1 < len(entries):
            following = find_best_match(
                entries[index + 1]["tokens"],
                asr_tokens,
                cursor,
            )
            if (
                following
                and following[0] >= MIN_LOOKAHEAD_SCORE
                and following[2] <= start
            ):
                entry.update(
                    status="not_found",
                    audio_start=None,
                    audio_end=None,
                    score=round(score, 3),
                )
                continue
        if score < MIN_CURSOR_SCORE:
            entry.update(status="not_found", audio_start=None, audio_end=None,
                         score=round(score, 3))
            continue
        while start < end and asr_tokens[start] not in target[:min(8, len(target))]:
            start += 1
        while end > start and asr_tokens[end - 1] not in target[-min(8, len(target)):]:
            end -= 1
        if end <= start:
            entry.update(status="not_found", audio_start=None, audio_end=None, score=round(score, 3))
            continue
        entry.update(audio_start=float(words[start]["start"]), audio_end=float(words[end - 1]["end"]),
                     score=round(score, 3), status="ok" if score >= 0.45 else "low_confidence")
        cursor = end
    return entries


def detect_silences(wav_path):
    result = run(["ffmpeg", "-hide_banner", "-i", str(wav_path), "-af",
                  f"silencedetect=noise={NOISE_DB}dB:d={MIN_SILENCE}", "-f", "null", "-"])
    starts, silences = [], []
    for line in result.stderr.splitlines():
        if match := re.search(r"silence_start:\s*([0-9.]+)", line):
            starts.append(float(match.group(1)))
        if (match := re.search(r"silence_end:\s*([0-9.]+)\s*\|\s*silence_duration:\s*([0-9.]+)", line)) and starts:
            silences.append((starts.pop(0), float(match.group(1)), float(match.group(2))))
    return silences


def rank_silences(approximate, silences):
    candidates = []
    for start, end, duration in silences:
        midpoint = (start + end) / 2
        distance = 0 if start <= approximate <= end else min(abs(approximate - start), abs(approximate - end))
        if distance <= SEARCH_RADIUS:
            score = distance + 0.20 * abs(midpoint - approximate) - 0.12 * min(duration, 0.8)
            candidates.append((score, start, end))
    return [(start, end) for _, start, end in sorted(candidates)]


def snap_boundaries(entries, silences):
    aligned = [entry for entry in entries if entry.get("audio_start") is not None]
    originals = {
        id(entry): (entry["audio_start"], entry["audio_end"])
        for entry in aligned
    }
    for previous, following in zip(aligned, aligned[1:]):
        if previous.get("_locked") or following.get("_locked"):
            continue
        if previous.get("status") == "manual" or following.get("status") == "manual":
            previous.setdefault("boundary_status", "word")
            following.setdefault("boundary_status", "word")
            continue
        approximate = (previous["audio_end"] + following["audio_start"]) / 2
        matched = False
        for silence in rank_silences(approximate, silences):
            if (silence[0] >= previous["audio_start"]
                    and silence[1] <= following["audio_end"]
                    and abs(silence[0] - previous["audio_end"]) <= SEARCH_RADIUS
                    and abs(silence[1] - following["audio_start"]) <= SEARCH_RADIUS
                    and silence[0] <= silence[1]):
                previous["audio_end"] = silence[0]
                following["audio_start"] = silence[1]
                previous["boundary_status"] = following["boundary_status"] = "silence"
                matched = True
                break
        if not matched:
            previous.setdefault("boundary_status", "word")
            following.setdefault("boundary_status", "word")
    for index, entry in enumerate(aligned):
        if entry.get("_locked") or entry.get("status") == "manual":
            continue
        original_start, original_end = originals[id(entry)]
        changed = False
        previous = aligned[index - 1] if index else None
        following = aligned[index + 1] if index + 1 < len(aligned) else None
        start_candidates = [
            silence for silence in silences
            if (not previous or (previous.get("status") != "manual" and not previous.get("_locked")))
            and silence[0] <= entry["audio_start"] <= silence[1]
        ]
        if start_candidates:
            entry["audio_start"] = max(start_candidates, key=lambda silence: silence[1])[1]
            changed = True
        end_candidates = [
            silence for silence in silences
            if (not following or (following.get("status") != "manual" and not following.get("_locked")))
            and silence[0] <= entry["audio_end"] <= silence[1]
        ]
        if end_candidates:
            entry["audio_end"] = min(end_candidates, key=lambda silence: silence[0])[0]
            changed = True
        if changed:
            if entry["audio_end"] - entry["audio_start"] >= MIN_SNAPPED_DURATION:
                entry["boundary_status"] = "silence"
            else:
                entry["boundary_status"] = "word"
                entry["audio_start"], entry["audio_end"] = original_start, original_end
    return entries


def atempo_filters(speed):
    filters = []
    while speed > 2.0:
        filters.append("atempo=2.0")
        speed /= 2.0
    if speed > 1.00001:
        filters.append(f"atempo={speed:.9f}")
    return filters




def cut_replacement_segment(project, replacement):
    assets = ensure_replacement_assets(project, replacement)
    source_wav = Path(settings.MEDIA_ROOT) / assets["source"]
    duration = float(assets["duration"])
    start = max(0.0, float(replacement.audio_start or 0))
    end = min(duration, float(replacement.audio_end or duration))
    if start >= end:
        raise ProcessingError("The selected replacement boundaries are invalid")
    work_dir = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id)
    segments_dir, fitted_dir = work_dir / "segments", work_dir / "fitted"
    segments_dir.mkdir(parents=True, exist_ok=True)
    fitted_dir.mkdir(parents=True, exist_ok=True)
    raw_path = segments_dir / f"{replacement.position:03d}.wav"
    fitted_path = fitted_dir / f"{replacement.position:03d}.wav"
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ss", f"{start:.6f}", "-t", f"{end-start:.6f}",
        "-i", str(source_wav), "-c:a", "pcm_s16le", str(raw_path),
    ])
    source_duration = probe_duration(raw_path)
    segment = project.segments.get(position=replacement.position)
    available = max(0.05, segment.video_end - segment.video_start - MIN_VIDEO_GAP)
    speed = max(1.0, source_duration / available)
    expected = min(source_duration / speed, available)
    filters = atempo_filters(speed) + [
        "afade=t=in:st=0:d=0.008",
        f"afade=t=out:st={max(0, expected-0.008):.6f}:d=0.008",
        f"atrim=duration={available:.6f}",
    ]
    run([
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-i", str(raw_path),
        "-af", ",".join(filters), "-ar", "44100", "-ac", "2",
        "-c:a", "pcm_s16le", str(fitted_path),
    ])
    segment.audio_start = start
    segment.audio_end = end
    segment.recognition_score = None
    segment.status = "replacement"
    segment.audio_file.name = raw_path.relative_to(settings.MEDIA_ROOT).as_posix()
    segment.source_duration = source_duration
    segment.output_duration = probe_duration(fitted_path)
    segment.speed = speed
    segment.save()
    project.output_file = ""
    project.save(update_fields=["output_file"])
def render_output(project, entries, fitted_files, video_duration, work_dir):
    output_dir = work_dir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    is_video = project.output_mode == project.OUTPUT_VIDEO
    output_path = output_dir / ("voiceover-video.mp4" if is_video else "voiceover-track.m4a")
    command = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
    if is_video:
        command.extend(["-i", project.video_file.path])
    silence_index = 1 if is_video else 0
    command.extend(["-f", "lavfi", "-t", f"{video_duration:.6f}", "-i", "anullsrc=r=44100:cl=stereo"])
    for path in fitted_files:
        command.extend(["-i", str(path)])
    filters, labels = [f"[{silence_index}:a]anull[base]"], ["[base]"]
    rendered = [entry for entry in entries if entry.get("fitted_path")]
    for offset, entry in enumerate(rendered):
        input_index = silence_index + 1 + offset
        label = f"[s{input_index}]"
        filters.append(f"[{input_index}:a]adelay={round(entry['video_start'] * 1000)}:all=1{label}")
        labels.append(label)
    filters.append("".join(labels) + f"amix=inputs={len(labels)}:duration=first:normalize=0[aout]")
    command.extend(["-filter_complex", ";".join(filters)])
    if is_video:
        command.extend(["-map", "0:v:0", "-map", "[aout]", "-c:v", "copy"])
    else:
        command.extend(["-map", "[aout]"])
    command.extend(["-c:a", "aac", "-b:a", "192k", "-t", f"{video_duration:.6f}",
                    "-movflags", "+faststart", str(output_path)])
    run(command)
    return output_path


def process_project(project, reuse_transcription=False, realign_from_position=None):
    if (not project.timecodes_text.strip() and not project.timecodes_file) or not project.video_file:
        raise ProcessingError("Audio, timecodes, and video are required")
    first_step = "aligning" if reuse_transcription else "preparing"
    project.processing_status, project.processing_step, project.processing_error = "processing", first_step, ""
    project.save(update_fields=["processing_status", "processing_step", "processing_error"])
    work_dir = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id)
    source_wav, groq_audio = work_dir / "source-44100.wav", work_dir / "source-groq.flac"
    transcription_path, alignment_path = work_dir / "transcription.json", work_dir / "alignment.json"
    segments_dir, fitted_dir = work_dir / "segments", work_dir / "fitted"
    for directory in (work_dir, segments_dir, fitted_dir):
        directory.mkdir(parents=True, exist_ok=True)
    try:
        if reuse_transcription:
            if not source_wav.exists():
                raise ProcessingError(
                    "Prepared audio is missing; start full processing manually"
                )
            if not transcription_path.exists():
                raise ProcessingError(
                    "Saved transcription is missing; start full processing manually"
                )
            payload = json.loads(transcription_path.read_text(encoding="utf-8"))
            words = payload.get("words") or []
            if not words:
                raise ProcessingError("Saved transcription has no word timestamps")
            validate_word_timestamps(words)
        else:
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", project.audio_file.path,
                 "-vn", "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(source_wav)])
            run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", project.audio_file.path,
                 "-vn", "-ar", "16000", "-ac", "1", "-c:a", "flac", str(groq_audio)])
            ensure_editor_assets(project, source_wav)
            update_progress(project, "transcribing")
            words = transcribe(groq_audio, transcription_path)["words"]
        video_duration = probe_duration(project.video_file.path)
        timecodes_text = project.timecodes_text.strip()
        if not timecodes_text:
            timecodes_text = Path(project.timecodes_file.path).read_text(encoding="utf-8-sig")
        entries = parse_timecodes(timecodes_text, video_duration)
        update_progress(project, "aligning")
        fixed_entries = {}
        affected_positions = {entry["id"] for entry in entries}
        if realign_from_position is not None:
            if not alignment_path.exists():
                raise ProcessingError(
                    "Saved alignment is missing; start full processing manually"
                )
            previous_alignment = json.loads(alignment_path.read_text(encoding="utf-8"))
            fixed_entries = {
                entry["id"]: entry
                for entry in previous_alignment
                if (
                    entry["id"] < realign_from_position
                    or (
                        entry["id"] > realign_from_position
                        and entry.get("status") in {"ok", "manual", "replacement"}
                        and entry.get("audio_start") is not None
                    )
                )
            }
            affected_positions -= fixed_entries.keys()
        overrides = {
            boundary.position: boundary
            for boundary in project.manual_boundaries.all()
        }
        entries = align_entries(entries, words, overrides=overrides, fixed_entries=fixed_entries)
        update_progress(project, "detecting")
        entries = snap_boundaries(entries, detect_silences(source_wav))
        for entry in entries:
            entry.pop("_locked", None)
        alignment_path.write_text(json.dumps(entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        update_progress(project, "cutting")
        if realign_from_position is None:
            project.segments.all().delete()
        existing_segments = {
            segment.position: segment for segment in project.segments.all()
        }
        for entry in entries:
            if entry["id"] not in affected_positions:
                continue
            segment = existing_segments.get(entry["id"]) or Segment(
                project=project, position=entry["id"]
            )
            segment.text = entry["text"]
            segment.video_start = entry["video_start"]
            segment.video_end = entry["video_end"]
            segment.audio_start = entry.get("audio_start")
            segment.audio_end = entry.get("audio_end")
            segment.recognition_score = entry.get("score")
            segment.status = entry["status"]
            segment.audio_file = ""
            segment.source_duration = segment.output_duration = None
            segment.speed = 1.0
            if entry.get("audio_start") is not None:
                start, end = float(entry["audio_start"]), float(entry["audio_end"])
                end = max(start + 0.01, end)
                raw_path = segments_dir / f"{entry['id']:03d}.wav"
                run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.6f}",
                     "-t", f"{end-start:.6f}", "-i", str(source_wav), "-c:a", "pcm_s16le", str(raw_path)])
                source_duration = probe_duration(raw_path)
                available = max(0.05, entry["video_end"] - entry["video_start"] - MIN_VIDEO_GAP)
                speed = max(1.0, source_duration / available)
                expected = min(source_duration / speed, available)
                filters = atempo_filters(speed) + ["afade=t=in:st=0:d=0.008",
                    f"afade=t=out:st={max(0, expected-0.008):.6f}:d=0.008", f"atrim=duration={available:.6f}"]
                fitted_path = fitted_dir / f"{entry['id']:03d}.wav"
                run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(raw_path),
                     "-af", ",".join(filters), "-ar", "44100", "-ac", "2", "-c:a", "pcm_s16le", str(fitted_path)])
                segment.audio_file.name = raw_path.relative_to(settings.MEDIA_ROOT).as_posix()
                segment.source_duration, segment.output_duration, segment.speed = source_duration, probe_duration(fitted_path), speed
            segment.save()
        for replacement in project.segment_replacements.all():
            if (
                realign_from_position is None or replacement.position in affected_positions
            ):
                cut_replacement_segment(project, replacement)
        if not project.segments.exclude(audio_file="").exists():
            raise ProcessingError(
                "None of the timecoded phrases could be aligned with the recording"
            )
        project.processing_status, project.processing_step, project.processing_error = "complete", "complete", ""
        project.transcription_file = transcription_path.relative_to(settings.MEDIA_ROOT).as_posix()
        project.output_file = ""
        project.save(update_fields=["processing_status", "processing_step", "processing_error", "transcription_file", "output_file"])
    except Exception as error:
        project.processing_status, project.processing_error = "failed", str(error)
        project.save(update_fields=["processing_status", "processing_error"])
        raise


def render_project_result(project):
    project.processing_status, project.processing_step, project.processing_error = "processing", "rendering", ""
    project.save(
        update_fields=["processing_status", "processing_step", "processing_error"]
    )
    work_dir = Path(settings.MEDIA_ROOT) / "jobs" / str(project.id)
    alignment_path = work_dir / "alignment.json"
    try:
        if not alignment_path.exists():
            raise ProcessingError("Saved alignment is missing; process fragments first")
        entries = json.loads(alignment_path.read_text(encoding="utf-8"))
        segments = {segment.position: segment for segment in project.segments.all()}
        fitted_files = []
        for entry in entries:
            segment = segments.get(entry["id"])
            if not segment or not segment.audio_file:
                continue
            fitted_path = work_dir / "fitted" / f"{entry['id']:03d}.wav"
            if not fitted_path.exists():
                continue
            entry["fitted_path"] = str(fitted_path)
            fitted_files.append(fitted_path)
        if not fitted_files:
            raise ProcessingError("There are no approved fragments to render")
        video_duration = probe_duration(project.video_file.path)
        output_path = render_output(
            project, entries, fitted_files, video_duration, work_dir
        )
        project.processing_status, project.processing_step, project.processing_error = "complete", "complete", ""
        project.output_file = output_path.relative_to(settings.MEDIA_ROOT).as_posix()
        project.save(
            update_fields=["processing_status", "processing_step", "processing_error", "output_file"]
        )
    except Exception as error:
        project.processing_status, project.processing_error = "failed", str(error)
        project.save(update_fields=["processing_status", "processing_error"])
        raise
