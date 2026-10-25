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

def localization_upload_to(instance, filename):
    return f"localizations/{instance.id}/source/{filename}"

def localization_version_upload_to(instance, filename):
    return f"localizations/{instance.localization_id}/{instance.language_code}/{filename}"

def takes_upload_to(instance, filename):
    return f"takes/{instance.id}/source/{filename}"

def take_recording_upload_to(instance, filename):
    return f"takes/{instance.takes_project_id}/recordings/{instance.position:03d}/{filename}"

def take_candidate_upload_to(instance, filename):
    return f"takes/{instance.phrase.takes_project_id}/candidates/{instance.phrase.position:03d}/{instance.id or uuid.uuid4()}.wav"

def media_tool_input_upload_to(instance, filename):
    return f"tools/jobs/{instance.job_id}/input/{instance.role}/{instance.position:03d}-{filename}"

def phrase_timing_video_upload_to(instance, filename):
    return f"tools/phrase-timing/{instance.id}/input/{filename}"

class Project(models.Model):
    OUTPUT_VIDEO = "video"
    OUTPUT_AUDIO = "audio"
    OUTPUT_CHOICES = [(OUTPUT_VIDEO, _("Video with the voiceover")), (OUTPUT_AUDIO, _("Audio track for YouTube"))]
    FORMAT_M4A = "m4a"
    FORMAT_WAV = "wav"
    FORMAT_CHOICES = [(FORMAT_M4A, _("M4A — compact")), (FORMAT_WAV, _("WAV — lossless"))]
    RETENTION_CHOICES = [
        (7, _("7 days")),
        (30, _("30 days")),
        (90, _("90 days")),
        (0, _("Keep until I delete it")),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        "auth.User",
        on_delete=models.CASCADE,
        related_name="voiceover_projects",
        null=True,
        blank=True,
    )
    audio_file = models.FileField(upload_to=audio_upload_to, blank=True, null=True)
    audio_revision = models.UUIDField(default=uuid.uuid4, editable=False)
    timecodes_text = models.TextField(blank=True)
    timecodes_file = models.FileField(upload_to=timecodes_upload_to, blank=True, null=True)
    video_file = models.FileField(upload_to=video_upload_to, blank=True, null=True)
    timing_project = models.OneToOneField(
        "PhraseTimingProject",
        on_delete=models.SET_NULL,
        related_name="voiceover_project",
        null=True,
        blank=True,
    )
    output_mode = models.CharField(max_length=20, choices=OUTPUT_CHOICES, default=OUTPUT_AUDIO)
    export_format = models.CharField(max_length=10, choices=FORMAT_CHOICES, default=FORMAT_M4A)
    normalize_loudness = models.BooleanField(default=True)
    speed_warning_threshold = models.FloatField(default=1.12)
    retention_days = models.PositiveSmallIntegerField(choices=RETENTION_CHOICES, default=30)
    processing_status = models.CharField(max_length=20, default="pending")
    processing_step = models.CharField(max_length=30, default="pending")
    processing_error = models.TextField(blank=True)
    transcription_file = models.CharField(max_length=500, blank=True)
    transcription_audio_revision = models.UUIDField(null=True, blank=True, editable=False)
    output_file = models.CharField(max_length=500, blank=True)
    export_manifest = models.JSONField(default=dict, blank=True)
    pending_reprocessing = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_opened_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return str(self.id)

    @property
    def audio_name(self):
        return Path(self.audio_file.name).name if self.audio_file else ""

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
    original_volume = models.FloatField(default=0.35)
    voice_volume = models.FloatField(default=1.0)
    duck_original = models.BooleanField(default=False)
    start_offset = models.FloatField(default=0)
    trim_to_video = models.BooleanField(default=True)
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


class ProcessingJob(models.Model):
    KIND_PROCESS = "process"
    KIND_RENDER = "render"
    KIND_CHOICES = [(KIND_PROCESS, _("Process project")), (KIND_RENDER, _("Render project"))]
    STATUS_CHOICES = [
        ("queued", _("Queued")),
        ("running", _("Running")),
        ("complete", _("Complete")),
        ("failed", _("Failed")),
        ("cancelled", _("Cancelled")),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="jobs")
    kind = models.CharField(max_length=20, choices=KIND_CHOICES)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="queued", db_index=True)
    payload = models.JSONField(default=dict, blank=True)
    progress = models.PositiveSmallIntegerField(default=0)
    current_step = models.CharField(max_length=40, blank=True)
    attempts = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=3)
    cancel_requested = models.BooleanField(default=False)
    error = models.TextField(blank=True)
    worker_pid = models.PositiveIntegerField(null=True, blank=True)
    heartbeat_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]


class StorageReservation(models.Model):
    key = models.CharField(max_length=120, unique=True)
    reserved_bytes = models.PositiveBigIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["created_at"]


class UsageRecord(models.Model):
    project = models.ForeignKey(Project, on_delete=models.CASCADE, related_name="usage_records")
    job = models.ForeignKey(ProcessingJob, on_delete=models.SET_NULL, null=True, blank=True, related_name="usage_records")
    owner = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="voiceover_usage")
    operation = models.CharField(max_length=30)
    media_seconds = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["job", "operation"],
                name="unique_usage_operation_per_job",
            )
        ]


class BillingAccount(models.Model):
    owner = models.OneToOneField("auth.User", on_delete=models.CASCADE, related_name="voiceover_billing")
    plan = models.CharField(max_length=30, default="free")
    monthly_minutes = models.PositiveIntegerField(default=30)
    extra_minutes = models.PositiveIntegerField(default=0)
    period_started_at = models.DateTimeField(auto_now_add=True)
    external_customer_id = models.CharField(max_length=120, blank=True)
    external_subscription_id = models.CharField(max_length=120, blank=True)


class LocalizationProject(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="localization_projects")
    title = models.CharField(max_length=200, blank=True)
    source_video = models.FileField(upload_to=localization_upload_to)
    source_script = models.FileField(upload_to=localization_upload_to, blank=True, null=True)
    source_script_text = models.TextField(blank=True)
    copy_settings = models.BooleanField(default=True)
    export_format = models.CharField(max_length=10, choices=Project.FORMAT_CHOICES, default=Project.FORMAT_M4A)
    normalize_loudness = models.BooleanField(default=True)
    speed_warning_threshold = models.FloatField(default=1.12)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def display_name(self):
        return self.title or Path(self.source_video.name).stem


class LocalizationVersion(models.Model):
    localization = models.ForeignKey(LocalizationProject, on_delete=models.CASCADE, related_name="versions")
    language_code = models.CharField(max_length=16)
    language_name = models.CharField(max_length=80)
    script_file = models.FileField(upload_to=localization_version_upload_to, blank=True, null=True)
    script_text = models.TextField(blank=True)
    audio_file = models.FileField(upload_to=localization_version_upload_to, blank=True, null=True)
    project = models.OneToOneField(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name="localization_version")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["id"]
        constraints = [models.UniqueConstraint(fields=["localization", "language_code"], name="unique_localization_language")]

    @property
    def status(self):
        if self.project_id:
            return self.project.processing_status
        if self.audio_file and (self.script_file or self.script_text.strip()):
            return "ready"
        return "missing"


class TakesProject(models.Model):
    MODE_LINES = "lines"
    MODE_READINGS = "readings"
    MODE_AUTO = "auto"
    MODE_CHOICES = [
        (MODE_LINES, _("Each phrase was repeated several times")),
        (MODE_READINGS, _("The complete text was read several times")),
        (MODE_AUTO, _("Determine automatically")),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey("auth.User", on_delete=models.CASCADE, related_name="takes_projects")
    title = models.CharField(max_length=200, blank=True)
    video_file = models.FileField(upload_to=takes_upload_to)
    script_file = models.FileField(upload_to=takes_upload_to, blank=True, null=True)
    script_text = models.TextField(blank=True)
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default=MODE_LINES)
    processing_status = models.CharField(max_length=20, default="pending")
    processing_step = models.CharField(max_length=30, default="pending")
    processing_error = models.TextField(blank=True)
    assembled_project = models.OneToOneField(Project, on_delete=models.SET_NULL, null=True, blank=True, related_name="takes_source")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def display_name(self):
        return self.title or Path(self.video_file.name).stem


class TakeRecording(models.Model):
    takes_project = models.ForeignKey(TakesProject, on_delete=models.CASCADE, related_name="recordings")
    position = models.PositiveIntegerField()
    media_file = models.FileField(upload_to=take_recording_upload_to)
    transcription_file = models.CharField(max_length=500, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["takes_project", "position"], name="unique_take_recording_position")]


class TakePhrase(models.Model):
    takes_project = models.ForeignKey(TakesProject, on_delete=models.CASCADE, related_name="phrases")
    position = models.PositiveIntegerField()
    text = models.TextField()
    video_start = models.FloatField()
    video_end = models.FloatField()
    status = models.CharField(max_length=30, default="pending")

    class Meta:
        ordering = ["position"]
        constraints = [models.UniqueConstraint(fields=["takes_project", "position"], name="unique_take_phrase_position")]


class TakeCandidate(models.Model):
    phrase = models.ForeignKey(TakePhrase, on_delete=models.CASCADE, related_name="candidates")
    recording = models.ForeignKey(TakeRecording, on_delete=models.CASCADE, related_name="candidates")
    source_start = models.FloatField()
    source_end = models.FloatField()
    score = models.FloatField(default=0)
    audio_file = models.FileField(upload_to=take_candidate_upload_to, blank=True)
    selected = models.BooleanField(default=False)
    rejected = models.BooleanField(default=False)
    manual = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["phrase__position", "recording__position", "source_start"]

    @property
    def duration(self):
        return max(0, self.source_end - self.source_start)


class MediaToolJob(models.Model):
    KIND_TRANSCRIBE = "audio-to-text"
    KIND_REPLACE = "replace-fragment"
    KIND_SPLIT = "split-phrases"
    KIND_FIT = "fit-subtitles"
    KIND_PREPARE = "prepare-voiceover"
    KIND_DURATIONS = "check-durations"
    KIND_SUBTITLES = "create-subtitles"
    KIND_CHOICES = [
        (KIND_TRANSCRIBE, _("Audio to text")),
        (KIND_REPLACE, _("Replace a voiceover fragment")),
        (KIND_SPLIT, _("Split voiceover into phrases")),
        (KIND_FIT, _("Fit audio to subtitles")),
        (KIND_PREPARE, _("Prepare voiceover")),
        (KIND_DURATIONS, _("Check phrase durations")),
        (KIND_SUBTITLES, _("Create synchronized subtitles")),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey("auth.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="media_tool_jobs")
    kind = models.CharField(max_length=40, choices=KIND_CHOICES)
    options = models.JSONField(default=dict, blank=True)
    processing_status = models.CharField(max_length=20, default="pending")
    processing_step = models.CharField(max_length=40, default="pending")
    processing_error = models.TextField(blank=True)
    result_file = models.CharField(max_length=500, blank=True)
    result_manifest = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    @property
    def output_name(self):
        return Path(self.result_file).name if self.result_file else ""


class MediaToolInput(models.Model):
    job = models.ForeignKey(MediaToolJob, on_delete=models.CASCADE, related_name="inputs")
    role = models.CharField(max_length=30)
    position = models.PositiveIntegerField(default=0)
    media_file = models.FileField(upload_to=media_tool_input_upload_to)

    class Meta:
        ordering = ["role", "position"]


class PhraseTimingProject(models.Model):
    MODE_MANUAL = "manual"
    MODE_AUTOMATIC = "automatic"
    MODE_CHOICES = [
        (MODE_MANUAL, _("Manual phrase timing")),
        (MODE_AUTOMATIC, _("Automatic phrase timing")),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        "auth.User", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="phrase_timing_projects",
    )
    video_file = models.FileField(upload_to=phrase_timing_video_upload_to)
    source_text = models.TextField(blank=True)
    mode = models.CharField(max_length=20, choices=MODE_CHOICES, default=MODE_MANUAL)
    phrases = models.JSONField(default=list)
    review_items = models.JSONField(default=list, blank=True)
    processing_status = models.CharField(max_length=20, default="complete")
    processing_step = models.CharField(max_length=40, default="complete")
    processing_error = models.TextField(blank=True)
    video_duration = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    @property
    def video_name(self):
        return Path(self.video_file.name).name

    @property
    def media_name(self):
        return self.video_name

    @property
    def media_kind(self):
        suffix = Path(self.video_file.name).suffix.lower()
        return "video" if suffix in {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"} else "audio"
