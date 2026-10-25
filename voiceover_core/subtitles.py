"""Parse script formats without depending on Django or storage models."""

from __future__ import annotations

import csv
import io
import re
from pathlib import Path


class SubtitleParseError(ValueError):
    pass


_CLOCK = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2}(?:[.,]\d+)?)$")
_LEGACY = re.compile(r"^(\d+):(\d{2}(?:[.,]\d+)?)(?::(\d{2}(?:[.,]\d+)?))?\s*(?:[-–—]\s*)?(.*)$")
_TIMELINE = re.compile(r"^\s*(\S+)\s*-->\s*(\S+)")


def parse_clock(value: str) -> float:
    clean = str(value).strip().replace(",", ".")
    match = _CLOCK.match(clean)
    if not match:
        try:
            return float(clean)
        except ValueError as error:
            raise SubtitleParseError(f"Invalid time value: {value}") from error
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes) * 60 + float(seconds)


def _finish(entries: list[dict], video_duration: float | None) -> list[dict]:
    if not entries:
        raise SubtitleParseError("The script contains no phrases")
    for index, entry in enumerate(entries):
        if not entry.get("text", "").strip():
            raise SubtitleParseError(f"No phrase at item {index + 1}")
        if index and entry["video_start"] < entries[index - 1]["video_end"] - 0.001:
            raise SubtitleParseError("Subtitle intervals overlap")
        if entry.get("video_end") is None:
            if index + 1 < len(entries):
                entry["video_end"] = entries[index + 1]["video_start"]
            elif video_duration is not None:
                entry["video_end"] = float(video_duration)
            else:
                entry["video_end"] = entry["video_start"] + 3.0
        if entry["video_end"] <= entry["video_start"]:
            raise SubtitleParseError(f"Invalid interval at item {index + 1}")
        entry["id"] = index + 1
        entry["text"] = re.sub(r"\s+", " ", entry["text"]).strip()
    return entries


def parse_srt_or_vtt(text: str, video_duration: float | None = None) -> list[dict]:
    lines = text.lstrip("\ufeff").replace("\r\n", "\n").split("\n")
    entries, index = [], 0
    while index < len(lines):
        line = lines[index].strip()
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "REGION")):
            index += 1
            continue
        if line.isdigit() and index + 1 < len(lines):
            index += 1
            line = lines[index].strip()
        match = _TIMELINE.match(line)
        if not match:
            index += 1
            continue
        start, end = parse_clock(match.group(1)), parse_clock(match.group(2))
        index += 1
        body = []
        while index < len(lines) and lines[index].strip():
            body.append(re.sub(r"<[^>]+>", "", lines[index].strip()))
            index += 1
        entries.append({"video_start": start, "video_end": end, "text": " ".join(body)})
    return _finish(entries, video_duration)


def parse_csv_script(text: str, video_duration: float | None = None) -> list[dict]:
    sample = text.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(sample[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = list(csv.reader(io.StringIO(sample), dialect))
    if not rows:
        return _finish([], video_duration)
    aliases = {
        "start": {"start", "begin", "in", "video_start", "начало", "старт"},
        "end": {"end", "finish", "out", "video_end", "конец"},
        "text": {"text", "phrase", "subtitle", "caption", "текст", "фраза"},
    }
    normalized = [cell.strip().lower() for cell in rows[0]]
    columns = {name: next((i for i, value in enumerate(normalized) if value in values), None) for name, values in aliases.items()}
    has_header = columns["start"] is not None and columns["text"] is not None
    data_rows = rows[1:] if has_header else rows
    if not has_header:
        columns = {"start": 0, "end": 1 if len(rows[0]) >= 3 else None, "text": 2 if len(rows[0]) >= 3 else 1}
    entries = []
    for line_number, row in enumerate(data_rows, 2 if has_header else 1):
        if not row or not any(cell.strip() for cell in row):
            continue
        try:
            start = parse_clock(row[columns["start"]])
            end = parse_clock(row[columns["end"]]) if columns["end"] is not None and row[columns["end"]].strip() else None
            phrase = row[columns["text"]].strip()
        except (IndexError, SubtitleParseError) as error:
            raise SubtitleParseError(f"Invalid CSV row {line_number}") from error
        entries.append({"video_start": start, "video_end": end, "text": phrase, "line": line_number})
    return _finish(entries, video_duration)


def parse_legacy_script(text: str, video_duration: float | None = None) -> list[dict]:
    entries, current = [], None
    for line_number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        match = _LEGACY.match(line)
        if match:
            if current:
                entries.append(current)
            first, second, third, phrase = match.groups()
            second = second.replace(",", ".")
            third = third.replace(",", ".") if third else None
            seconds = int(first) * 60 + float(second) if third is None else int(first) * 3600 + int(float(second)) * 60 + float(third)
            current = {"video_start": float(seconds), "video_end": None, "text": phrase.strip(), "line": line_number}
        elif current:
            current["text"] = f"{current['text']} {line}".strip()
        else:
            raise SubtitleParseError(f"Text before the first timecode on line {line_number}")
    if current:
        entries.append(current)
    return _finish(entries, video_duration)


def parse_script(text: str, filename: str = "", video_duration: float | None = None) -> list[dict]:
    suffix = Path(filename).suffix.lower()
    clean = text.lstrip("\ufeff")
    if suffix in {".srt", ".vtt"} or "-->" in clean:
        return parse_srt_or_vtt(clean, video_duration)
    if suffix == ".csv":
        return parse_csv_script(clean, video_duration)
    return parse_legacy_script(clean, video_duration)


def format_srt(entries: list[dict]) -> str:
    def clock(seconds: float) -> str:
        milliseconds = round(max(0, seconds) * 1000)
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    blocks = [f"{index}\n{clock(entry['video_start'])} --> {clock(entry['video_end'])}\n{entry['text']}" for index, entry in enumerate(entries, 1)]
    return "\n\n".join(blocks) + "\n"


def format_vtt(entries: list[dict]) -> str:
    def clock(seconds: float) -> str:
        milliseconds = round(max(0, seconds) * 1000)
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        secs, millis = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"

    blocks = [
        f"{clock(entry['video_start'])} --> {clock(entry['video_end'])}\n{entry['text']}"
        for entry in entries
    ]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + "\n"


def parse_plain_phrases(text: str) -> list[dict]:
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.lstrip("\ufeff").splitlines()]
    phrases = [line for line in lines if line]
    if not phrases:
        raise SubtitleParseError("The script contains no phrases")
    return [{"id": index, "text": phrase} for index, phrase in enumerate(phrases, 1)]


def parse_manual_ranges(text: str) -> list[dict]:
    """Parse one manually selected audio interval per line.

    Accepted examples::

        00:00.250 --> 00:03.800 First phrase
        4.1 - 7.6

    A label is optional. Ranges must be ordered and must not overlap.
    """
    entries = []
    pattern = re.compile(r"^\s*(\S+)\s*(?:-->|[-–—])\s*(\S+)(?:\s+(.+?))?\s*$")
    for line_number, raw in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        match = pattern.match(line)
        if not match:
            raise SubtitleParseError(f"Invalid manual interval on line {line_number}")
        start = parse_clock(match.group(1))
        end = parse_clock(match.group(2))
        if end <= start:
            raise SubtitleParseError(f"Invalid manual interval on line {line_number}")
        if entries and start < entries[-1]["video_end"] - .001:
            raise SubtitleParseError(f"Manual intervals overlap on line {line_number}")
        entries.append({
            "id": len(entries) + 1,
            "video_start": start,
            "video_end": end,
            "text": (match.group(3) or "").strip(),
        })
    if not entries:
        raise SubtitleParseError("No manual intervals were entered")
    return entries
