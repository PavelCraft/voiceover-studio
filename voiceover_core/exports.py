"""Create portable sidecar files without Django dependencies."""

from __future__ import annotations

import csv
from html import escape
from pathlib import Path
from zipfile import ZIP_STORED, ZipFile

from .subtitles import format_srt


def create_sidecar_exports(output_dir: Path, entries: list[dict], clips: dict[int, Path]) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    srt_path = output_dir / "voiceover-subtitles.srt"
    srt_path.write_text(format_srt(entries), encoding="utf-8")

    csv_path = output_dir / "voiceover-timeline.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["position", "start", "end", "text", "clip"])
        for entry in entries:
            clip = clips.get(int(entry["id"]))
            writer.writerow([entry["id"], entry["video_start"], entry["video_end"], entry["text"], clip.name if clip else ""])

    duration = max((float(entry["video_end"]) for entry in entries), default=0)
    fcpxml_path = output_dir / "voiceover-timeline.fcpxml"
    clips_xml = []
    for entry in entries:
        clip = clips.get(int(entry["id"]))
        if not clip:
            continue
        start = round(float(entry["video_start"]) * 1000)
        length = max(1, round((float(entry["video_end"]) - float(entry["video_start"])) * 1000))
        clips_xml.append(
            f'<asset-clip name="{escape(entry["text"][:80], quote=True)}" ref="r{entry["id"]}" '
            f'offset="{start}/1000s" start="0s" duration="{length}/1000s" lane="1"/>'
        )
    resources = "".join(
        f'<asset id="r{position}" name="clip-{position:03d}" src="clips/{position:03d}{path.suffix.lower()}" hasAudio="1"/>'
        for position, path in sorted(clips.items()) if path.exists()
    )
    fcpxml_path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<fcpxml version="1.10"><resources><format id="r0" name="FFVideoFormat1080p25" frameDuration="1/25s"/>'
        f'{resources}</resources><library><event name="Voiceover Studio"><project name="Voiceover">'
        f'<sequence format="r0" duration="{round(duration * 1000)}/1000s"><spine>{"".join(clips_xml)}</spine>'
        '</sequence></project></event></library></fcpxml>\n',
        encoding="utf-8",
    )
    return {"srt": srt_path, "csv": csv_path, "fcpxml": fcpxml_path}


def create_clips_archive(output_dir: Path, clips: dict[int, Path]) -> Path:
    zip_path = output_dir / "voiceover-clips.zip"
    sidecars = [
        output_dir / "voiceover-subtitles.srt",
        output_dir / "voiceover-timeline.csv",
        output_dir / "voiceover-timeline.fcpxml",
    ]
    with ZipFile(zip_path, "w", compression=ZIP_STORED) as archive:
        for sidecar in sidecars:
            if sidecar.exists():
                archive.write(sidecar, sidecar.name)
        for position, clip in sorted(clips.items()):
            if clip.exists():
                archive.write(clip, f"clips/{position:03d}{clip.suffix.lower()}")
    return zip_path
