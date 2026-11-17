"""Pure FFmpeg filter and command preparation helpers."""


MP3_BITRATES = ("128", "192", "256", "320")
DEFAULT_MP3_BITRATE = "192"
MIN_AUDIO_EDIT_SPEED = 0.000001
MAX_AUDIO_EDIT_SPEED = 1_000_000


def normalize_mp3_bitrate(value) -> str:
    bitrate = str(value or DEFAULT_MP3_BITRATE)
    return bitrate if bitrate in MP3_BITRATES else DEFAULT_MP3_BITRATE


def mp3_codec_arguments(value=None) -> list[str]:
    return ["-c:a", "libmp3lame", "-b:a", f"{normalize_mp3_bitrate(value)}k"]


def atempo_filters(speed: float) -> list[str]:
    filters = []
    while speed < 0.5:
        filters.append("atempo=0.5")
        speed /= 0.5
    while speed > 2.0:
        filters.append("atempo=2.0")
        speed /= 2.0
    if abs(speed - 1.0) > 0.00001:
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
