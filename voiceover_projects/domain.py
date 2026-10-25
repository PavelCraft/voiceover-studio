from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ExportSettings:
    format: str = "m4a"
    normalize_loudness: bool = True
    target_lufs: float = -16.0
    speed_warning: float = 1.12


@dataclass(frozen=True)
class ProjectInput:
    audio_path: Path
    video_path: Path
    phrases: tuple[dict, ...]
    options: ExportSettings = field(default_factory=ExportSettings)


@dataclass
class SegmentData:
    position: int
    text: str
    video_start: float
    video_end: float
    audio_start: float | None = None
    audio_end: float | None = None
    score: float | None = None
    status: str = "pending"
    speed: float = 1.0
