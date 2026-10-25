"""Pure FFmpeg filter and command preparation helpers."""


def atempo_filters(speed: float) -> list[str]:
    filters = []
    while speed > 2.0:
        filters.append("atempo=2.0")
        speed /= 2.0
    if speed > 1.00001:
        filters.append(f"atempo={speed:.9f}")
    return filters


def fitting_filters(
    speed: float,
    expected: float,
    available: float,
    normalize_loudness: bool = True,
) -> list[str]:
    filters = atempo_filters(speed)
    if normalize_loudness:
        filters.append("loudnorm=I=-16:LRA=7:TP=-1.5")
    filters.extend([
        "afade=t=in:st=0:d=0.008",
        f"afade=t=out:st={max(0, expected - 0.008):.6f}:d=0.008",
        f"atrim=duration={available:.6f}",
    ])
    return filters


def video_codec_arguments(codec_name: str) -> list[str]:
    if codec_name in {"h264", "hevc", "mpeg4", "av1"}:
        return ["-c:v", "copy"]
    return ["-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p"]
