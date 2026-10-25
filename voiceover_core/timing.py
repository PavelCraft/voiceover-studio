"""Reusable phrase timing helpers, independent from Django and the web UI."""

from __future__ import annotations

import re
from difflib import SequenceMatcher


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_EDITABLE_TIMECODE = re.compile(
    r"^\[(?P<clock>(?:(?:\d+):)?\d+:\d{2}(?:[.,]\d{1,3})?)"
    r"(?:\s*-->\s*(?P<end_clock>(?:(?:\d+):)?\d+:\d{2}(?:[.,]\d{1,3})?))?\]\s*(?P<text>.*)$"
)
_COMMON_ABBREVIATIONS = {
    "т.", "д.", "т.е.", "т.к.", "г.", "ул.", "рис.", "им.",
    "mr.", "mrs.", "ms.", "dr.", "prof.", "e.g.", "i.e.",
}


def split_script(text: str) -> list[str]:
    """Split plain Russian or English text into editable phrase rows.

    Explicit line breaks are always preserved as phrase boundaries. Sentence
    punctuation creates additional rows while common abbreviations stay intact.
    """
    phrases: list[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = re.sub(r"[\t ]+", " ", raw_line).strip()
        if not line:
            continue
        start = 0
        for match in _SENTENCE_END.finditer(line):
            candidate = line[start:match.start()].strip()
            last_token = candidate.rsplit(" ", 1)[-1].lower() if candidate else ""
            if last_token in _COMMON_ABBREVIATIONS or re.search(r"(?:\b[A-Za-zА-Яа-яЁё]\.){1,}$", candidate):
                continue
            if candidate:
                phrases.append(candidate)
            start = match.end()
        tail = line[start:].strip()
        if tail:
            phrases.append(tail)
    return phrases


def normalize_timed_phrases(items: list[dict], duration: float | None = None) -> list[dict]:
    """Validate client timing data and derive safe end points."""
    result: list[dict] = []
    last_start = -1.0
    for index, item in enumerate(items, 1):
        text = re.sub(r"\s+", " ", str(item.get("text", ""))).strip()
        if not text:
            continue
        raw_start = item.get("start")
        start = None if raw_start in (None, "") else max(0.0, round(float(raw_start), 3))
        if start is not None and start < last_start:
            raise ValueError("Phrase timecodes must be ordered")
        if start is not None:
            last_start = start
        explicit_end = bool(item.get("end_explicit")) and item.get("end") not in (None, "")
        end = max(0.0, round(float(item["end"]), 3)) if explicit_end else None
        normalized = {
            "id": index,
            "text": text,
            "start": start,
            "end": end,
            "end_explicit": explicit_end,
        }
        for key in ("words", "issues", "source"):
            if key in item:
                normalized[key] = item[key]
        result.append(normalized)

    for index, item in enumerate(result):
        if item["start"] is None:
            item["end"] = None
            item["end_explicit"] = False
            continue
        next_start = next(
            (candidate["start"] for candidate in result[index + 1:] if candidate["start"] is not None),
            None,
        )
        if item["end_explicit"]:
            if item["end"] <= item["start"]:
                raise ValueError("A phrase end must be after its start")
            if duration is not None and item["end"] > float(duration):
                raise ValueError("A phrase end cannot be after the video ends")
        else:
            estimated = item["start"] + estimate_speech_duration(item["text"])
            if next_start is not None:
                item["end"] = next_start
            elif duration is not None:
                item["end"] = min(float(duration), estimated)
            else:
                item["end"] = estimated
        item["end"] = round(max(item["start"] + 0.05, item["end"]), 3)
    return result


def reconcile_phrase_texts(existing: list[dict], text: str) -> list[dict]:
    """Apply edited plain text while retaining timings wherever structure survives."""
    new_texts = split_script(text)
    old_texts = [str(item.get("text", "")).strip() for item in existing]
    result: list[dict] = []
    matcher = SequenceMatcher(a=old_texts, b=new_texts, autojunk=False)
    for tag, old_start, old_end, new_start, new_end in matcher.get_opcodes():
        if tag == "equal":
            for old_item, new_text in zip(existing[old_start:old_end], new_texts[new_start:new_end]):
                result.append({
                    "text": new_text,
                    "start": old_item.get("start"),
                    "end": old_item.get("end"),
                    "end_explicit": old_item.get("end_explicit", False),
                })
            continue
        replacement = new_texts[new_start:new_end]
        retained_start = existing[old_start].get("start") if old_start < old_end else None
        for offset, new_text in enumerate(replacement):
            result.append({"text": new_text, "start": retained_start if offset == 0 else None})
    return normalize_timed_phrases(result)


def _parse_editor_clock(value: str) -> float:
    clock_value = value.replace(",", ".")
    units = clock_value.split(":")
    if len(units) not in {2, 3}:
        raise ValueError(f"Invalid timecode: {value}")
    try:
        seconds = float(units[-1])
        minutes = int(units[-2])
        hours = int(units[0]) if len(units) == 3 else 0
    except ValueError as error:
        raise ValueError(f"Invalid timecode: {value}") from error
    if seconds >= 60 or (len(units) == 3 and minutes >= 60):
        raise ValueError(f"Invalid timecode: {value}")
    return round(hours * 3600 + minutes * 60 + seconds, 3)


def parse_editable_timing_text(text: str, duration: float | None = None) -> list[dict]:
    """Parse one phrase per line, with an optional editable ``[MM:SS.mmm]`` prefix."""
    items: list[dict] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        match = _EDITABLE_TIMECODE.match(line)
        if match:
            phrase_text = match.group("text").strip()
            if not phrase_text:
                raise ValueError("A timecode must be followed by phrase text")
            end_clock = match.group("end_clock")
            items.append({
                "text": phrase_text,
                "start": _parse_editor_clock(match.group("clock")),
                "end": _parse_editor_clock(end_clock) if end_clock else None,
                "end_explicit": bool(end_clock),
            })
        else:
            if line.startswith("["):
                raise ValueError(f"Invalid timecode in line: {line}")
            items.append({"text": line, "start": None})
    return normalize_timed_phrases(items, duration)


def format_editable_timing_text(items: list[dict]) -> str:
    """Format rows for editing, showing prefixes only for assigned timecodes."""
    lines: list[str] = []
    for item in items:
        phrase_text = str(item.get("text", "")).strip()
        if not phrase_text:
            continue
        start = item.get("start")
        if start in (None, ""):
            lines.append(phrase_text)
            continue
        total = max(0, round(float(start) * 1000))
        hours, remainder = divmod(total, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, millis = divmod(remainder, 1000)
        value = f"{minutes:02d}:{seconds:02d}.{millis:03d}"
        if hours:
            value = f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
        end = item.get("end")
        if item.get("end_explicit") and end not in (None, ""):
            end_total = max(0, round(float(end) * 1000))
            end_hours, end_remainder = divmod(end_total, 3_600_000)
            end_minutes, end_remainder = divmod(end_remainder, 60_000)
            end_seconds, end_millis = divmod(end_remainder, 1000)
            end_value = f"{end_minutes:02d}:{end_seconds:02d}.{end_millis:03d}"
            if end_hours:
                end_value = f"{end_hours:02d}:{end_minutes:02d}:{end_seconds:02d}.{end_millis:03d}"
            value = f"{value} --> {end_value}"
        lines.append(f"[{value}] {phrase_text}")
    return "\n".join(lines)


def estimate_speech_duration(text: str, words_per_minute: int = 160) -> float:
    words = re.findall(r"[\w'-]+(?:\.[\w'-]+)*", text, flags=re.UNICODE)
    punctuation_pause = (
        0.2 * len(re.findall(r"[,;:](?=\s|$)", text))
        + 0.35 * len(re.findall(r"[!?…]+|\.(?=\s|$)", text))
    )
    return max(0.8, len(words) * 60 / words_per_minute + punctuation_pause)


def format_clock(seconds: float, separator: str = ",") -> str:
    milliseconds = round(max(0, seconds) * 1000)
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def export_timed_phrases(
    items: list[dict],
    export_format: str,
    duration: float | None = None,
    include_unmarked: bool = False,
) -> str:
    phrases = normalize_timed_phrases(items, duration)
    if phrases and phrases[0]["start"] is None:
        raise ValueError("The first phrase must have a timecode before export")
    if include_unmarked:
        timed: list[dict] = []
        for item in phrases:
            if item["start"] is None:
                if timed:
                    timed[-1]["text"] = f"{timed[-1]['text']} {item['text']}".strip()
                continue
            exported = dict(item)
            timed.append(exported)
    else:
        timed = [item for item in phrases if item["start"] is not None]
    if not timed:
        raise ValueError("No phrase timecodes have been assigned")
    if export_format == "txt":
        return "\n".join(f"{format_clock(item['start'], '.')} - {item['text']}" for item in timed) + "\n"
    blocks = []
    for index, item in enumerate(timed, 1):
        separator = "," if export_format == "srt" else "."
        body = f"{format_clock(item['start'], separator)} --> {format_clock(item['end'], separator)}\n{item['text']}"
        blocks.append(f"{index}\n{body}" if export_format == "srt" else body)
    if export_format == "srt":
        return "\n\n".join(blocks) + "\n"
    if export_format == "vtt":
        return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"
    raise ValueError("Unsupported export format")
