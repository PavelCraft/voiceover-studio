"""Interfaces implemented by Groq, FFmpeg, storage and future desktop adapters."""

from pathlib import Path
from typing import Protocol


class Transcriber(Protocol):
    def transcribe(self, audio_path: Path) -> dict: ...


class CommandRunner(Protocol):
    def run(self, command: list[str]) -> str: ...


class ProgressReporter(Protocol):
    def update(self, step: str, progress: int) -> None: ...
    def is_cancelled(self) -> bool: ...
