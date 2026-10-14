import json
import subprocess
from pathlib import Path

from django.conf import settings


class MediaMergeError(RuntimeError):
    pass


def run(command):
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode:
        lines = result.stderr.strip().splitlines()
        raise MediaMergeError(
            lines[-1] if lines else "FFmpeg command failed"
        )
    return result


def probe_media(path):
    result = run([
        "ffprobe",
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ])
    return json.loads(result.stdout)


def update_step(job, step):
    job.processing_step = step
    job.__class__.objects.filter(pk=job.pk).update(processing_step=step)


def video_codec_arguments(codec_name):
    if codec_name == "h264":
        return ["-c:v", "copy"]
    return [
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        "yuv420p",
    ]


def merge_video_with_audio(job):
    job.processing_status = "processing"
    job.processing_step = "preparing"
    job.processing_error = ""
    job.save(
        update_fields=[
            "processing_status",
            "processing_step",
            "processing_error",
        ]
    )

    try:
        video_info = probe_media(job.video_file.path)
        audio_info = probe_media(job.audio_file.path)
        video_streams = [
            stream
            for stream in video_info.get("streams", [])
            if stream.get("codec_type") == "video"
        ]
        new_audio_streams = [
            stream
            for stream in audio_info.get("streams", [])
            if stream.get("codec_type") == "audio"
        ]
        original_audio_streams = [
            stream
            for stream in video_info.get("streams", [])
            if stream.get("codec_type") == "audio"
        ]
        if not video_streams:
            raise MediaMergeError("The selected video contains no video stream")
        if not new_audio_streams:
            raise MediaMergeError("The selected audio file contains no audio stream")

        try:
            duration = float(video_info["format"]["duration"])
        except (KeyError, TypeError, ValueError):
            raise MediaMergeError("Could not determine the video duration")

        output_dir = (
            Path(settings.MEDIA_ROOT)
            / "tools"
            / "video-audio"
            / str(job.id)
            / "output"
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        output_path = output_dir / "video-with-audio.mp4"

        update_step(job, "merging")
        command = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            job.video_file.path,
            "-i",
            job.audio_file.path,
        ]
        if job.mix_original and original_audio_streams:
            audio_filter = (
                "[0:a:0]aresample=48000,apad[original];"
                "[1:a:0]aresample=48000,apad[added];"
                "[original][added]"
                "amix=inputs=2:duration=longest:"
                "dropout_transition=0:normalize=0,"
                "alimiter=limit=0.95[aout]"
            )
        else:
            audio_filter = "[1:a:0]aresample=48000,apad[aout]"

        command.extend([
            "-filter_complex",
            audio_filter,
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
            *video_codec_arguments(video_streams[0].get("codec_name")),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ac",
            "2",
            "-t",
            f"{duration:.6f}",
            "-movflags",
            "+faststart",
            str(output_path),
        ])
        run(command)

        update_step(job, "finalizing")
        job.processing_status = "complete"
        job.processing_step = "complete"
        job.processing_error = ""
        job.output_file = output_path.relative_to(
            settings.MEDIA_ROOT
        ).as_posix()
        job.save(
            update_fields=[
                "processing_status",
                "processing_step",
                "processing_error",
                "output_file",
            ]
        )
    except Exception as error:
        job.processing_status = "failed"
        job.processing_error = str(error)
        job.save(
            update_fields=["processing_status", "processing_error"]
        )
        raise
