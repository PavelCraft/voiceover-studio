import uuid
from pathlib import Path
from django.db import models
from django.utils.translation import gettext_lazy as _

def audio_upload_to(instance, filename):
    return f"jobs/{instance.id}/input/audio/{filename}"

def video_upload_to(instance, filename):
    return f"jobs/{instance.id}/input/video/{filename}"

def timecodes_upload_to(instance, filename):
    return f"jobs/{instance.id}/input/timecodes/{filename}"

def segment_upload_to(instance, filename):
    return f"jobs/{instance.project_id}/segments/{filename}"

def replacement_audio_upload_to(instance, filename):
    return f"jobs/{instance.project_id}/replacements/{instance.position}/input/{filename}"

def merge_video_upload_to(instance, filename):
    return f"tools/video-audio/{instance.id}/input/video/{filename}"

def merge_audio_upload_to(instance, filename):
    return f"tools/video-audio/{instance.id}/input/audio/{filename}"

class Project(models.Model):
    OUTPUT_VIDEO = "video"
    OUTPUT_AUDIO = "audio"
    OUTPUT_CHOICES = [(OUTPUT_VIDEO, _("Video with the voiceover")), (OUTPUT_AUDIO, _("Audio track for YouTube"))]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="voiceover_projects",
        null=True,
        blank=True,
    )
    audio_file = models.FileField(upload_to=audio_upload_to)
    timecodes_text = models.TextField(blank=True)
    timecodes_file = models.FileField(upload_to=timecodes_upload_to, blank=True, null=True)
    video_file = models.FileField(upload_to=video_upload_to, blank=True, null=True)
    output_mode = models.CharField(max_length=20, choices=OUTPUT_CHOICES, default=OUTPUT_AUDIO)
    processing_status = models.CharField(max_length=20, default="pending")
    processing_step = models.CharField(max_length=30, default="pending")
    processing_error = models.TextField(blank=True)
    transcription_file = models.CharField(max_length=500, blank=True)
    output_file = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.id)

    @property
    def audio_name(self):
        return Path(self.audio_file.name).name

    @property
    def video_name(self):
        return Path(self.video_file.name).name if self.video_file else ""

    @property
    def timecodes_name(self):
        return Path(self.timecodes_file.name).name if self.timecodes_file else ""

    @property
    def output_name(self):
        return Path(self.output_file).name if self.output_file else ""


class Segment(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="segments")
    position = models.PositiveIntegerField()
    text = models.TextField()
    video_start = models.FloatField()
    video_end = models.FloatField()
    audio_start = models.FloatField(null=True, blank=True)
    audio_end = models.FloatField(null=True, blank=True)
    recognition_score = models.FloatField(null=True, blank=True)
    status = models.CharField(max_length=30, default="pending")
    audio_file = models.FileField(upload_to=segment_upload_to, blank=True)
    source_duration = models.FloatField(null=True, blank=True)
    output_duration = models.FloatField(null=True, blank=True)
    speed = models.FloatField(default=1.0)

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["project", "position"], name="unique_project_segment_position")]


class ManualSegmentBoundary(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="manual_boundaries",
    )
    position = models.PositiveIntegerField()
    audio_start = models.FloatField(null=True, blank=True)
    audio_end = models.FloatField(null=True, blank=True)
    skipped = models.BooleanField(default=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "position"],
                name="unique_manual_boundary_position",
            )
        ]


class SegmentReplacement(models.Model):
    project = models.ForeignKey(
        Project,
        on_delete=models.CASCADE,
        related_name="segment_replacements",
    )
    position = models.PositiveIntegerField()
    audio_file = models.FileField(upload_to=replacement_audio_upload_to)
    audio_start = models.FloatField(default=0)
    audio_end = models.FloatField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["position"]
        constraints = [
            models.UniqueConstraint(
                fields=["project", "position"], name="unique_segment_replacement"
            )
        ]

class VideoAudioMergeJob(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    video_file = models.FileField(upload_to=merge_video_upload_to)
    audio_file = models.FileField(upload_to=merge_audio_upload_to)
    mix_original = models.BooleanField(default=False)
    processing_status = models.CharField(max_length=20, default="pending")
    processing_step = models.CharField(max_length=30, default="pending")
    processing_error = models.TextField(blank=True)
    output_file = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return str(self.id)

    @property
    def video_name(self):
        return Path(self.video_file.name).name

    @property
    def audio_name(self):
        return Path(self.audio_file.name).name

    @property
    def output_name(self):
        return Path(self.output_file).name if self.output_file else ""
