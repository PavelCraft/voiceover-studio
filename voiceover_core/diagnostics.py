"""Problem classification shared by web and future desktop clients."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DiagnosticOptions:
    low_confidence: float = 0.45
    speed_warning: float = 1.12


def diagnose_segments(segments, options: DiagnosticOptions | None = None):
    options = options or DiagnosticOptions()
    results, previous_end = [], None
    for item in segments:
        get = item.get if isinstance(item, dict) else lambda name, default=None: getattr(item, name, default)
        start, end, status = get("audio_start"), get("audio_end"), get("status", "")
        score = get("recognition_score", get("score"))
        speed = float(get("speed", 1) or 1)
        problems = []
        if status != "skipped" and (status == "not_found" or start is None or end is None):
            problems.append("not_found")
        if score is not None and float(score) < options.low_confidence:
            problems.append("low_confidence")
        if speed > options.speed_warning + 1e-6:
            problems.append("speed")
        if previous_end is not None and start is not None and float(start) < float(previous_end) - 0.001:
            problems.append("overlap")
        if end is not None:
            previous_end = end
        results.append(problems)
    return results
