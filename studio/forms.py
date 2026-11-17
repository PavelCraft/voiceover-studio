import re
import tempfile
from pathlib import Path
from django import forms
from django.utils.translation import gettext_lazy as _
from .models import LocalizationProject, PhraseTimingProject, Project, TakesProject, VideoAudioMergeJob
from voiceover_core.media import DEFAULT_MP3_BITRATE, normalize_mp3_bitrate
from voiceover_core.subtitles import SubtitleParseError, parse_manual_ranges, parse_script
from .services.voiceover import ProcessingError, probe_has_audio

AUDIO_EXTENSIONS = ".mp3, .wav, .webm, .ogg, .m4a, .aac, .flac, .mp4, .mkv"
VIDEO_EXTENSIONS = ".mp4, .webm, .mkv, .mov, .avi, .m4v"
ALLOWED_AUDIO = {".mp3", ".wav", ".webm", ".ogg", ".m4a", ".aac", ".flac", ".mp4", ".mkv"}
ALLOWED_VIDEO = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"}
ALLOWED_SCRIPTS = {".txt", ".srt", ".vtt", ".csv"}

LANGUAGE_CHOICES = [
    ("en", _("English")), ("ru", _("Russian")), ("es", _("Spanish")),
    ("de", _("German")), ("fr", _("French")), ("pt-BR", _("Portuguese (Brazil)")),
    ("pt-PT", _("Portuguese (Portugal)")), ("ja", _("Japanese")),
    ("zh-CN", _("Chinese (Simplified)")), ("it", _("Italian")),
    ("pl", _("Polish")), ("tr", _("Turkish")), ("uk", _("Ukrainian")),
]


class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True


class MultipleFileField(forms.FileField):
    widget = MultipleFileInput

    def clean(self, data, initial=None):
        clean_one = super().clean
        if isinstance(data, (list, tuple)):
            return [clean_one(item, initial) for item in data]
        return [clean_one(data, initial)] if data else []


def validate_extension(upload, allowed, message):
    if upload and Path(upload.name).suffix.lower() not in allowed:
        raise forms.ValidationError(message)
    return upload


def mp3_quality_field():
    return forms.ChoiceField(
        required=False,
        choices=[
            ("128", _("128 kbit/s — smaller file")),
            ("192", _("192 kbit/s — recommended")),
            ("256", _("256 kbit/s — high quality")),
            ("320", _("320 kbit/s — maximum quality")),
        ],
        initial=DEFAULT_MP3_BITRATE,
        label=_("MP3 quality"),
        help_text=_("A higher bitrate preserves more audio detail but increases the file size."),
    )


class Mp3QualityFormMixin:
    """Add one shared MP3-quality field immediately after output_format."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        quality = mp3_quality_field()
        reordered = {}
        for name, field in self.fields.items():
            reordered[name] = field
            if name == "output_format":
                reordered["mp3_bitrate"] = quality
        if "mp3_bitrate" not in reordered:
            reordered["mp3_bitrate"] = quality
        self.fields = reordered

    def clean_mp3_bitrate(self):
        return normalize_mp3_bitrate(self.cleaned_data.get("mp3_bitrate"))

class AudioUploadForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["audio_file", "timecodes_text", "timecodes_file"]
        labels = {
            "audio_file": _("Voiceover recording"),
            "timecodes_text": _("Text with video timecodes"),
            "timecodes_file": _("Or upload TXT, SRT, VTT or CSV"),
        }
        widgets = {
            "audio_file": forms.FileInput(attrs={"accept": "audio/*,video/mp4,video/webm,.mkv"}),
            "timecodes_text": forms.Textarea(attrs={"rows": 12, "placeholder": "00:00 - First phrase\n00:08 - Second phrase"}),
            "timecodes_file": forms.FileInput(attrs={"accept": ".txt,.srt,.vtt,.csv,text/plain,text/vtt,text/csv"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["audio_file"].required = not bool(self.instance.pk and self.instance.audio_file)
        self.fields["timecodes_text"].required = False
        self.fields["timecodes_file"].required = False

    def clean_audio_file(self):
        file = self.cleaned_data.get("audio_file")
        if not file:
            return file
        extension = Path(file.name).suffix.lower()
        if extension not in ALLOWED_AUDIO:
            raise forms.ValidationError(_("Unsupported audio format. Allowed formats: %(formats)s.") % {"formats": AUDIO_EXTENSIONS})
        return file

    def clean_timecodes_file(self):
        file = self.cleaned_data.get("timecodes_file")
        if file and Path(file.name).suffix.lower() not in ALLOWED_SCRIPTS:
            raise forms.ValidationError(_("Unsupported script format. Upload TXT, SRT, VTT or CSV."))
        return file

    def clean(self):
        cleaned = super().clean()
        has_existing_file = bool(self.instance.pk and self.instance.timecodes_file)
        if not cleaned.get("timecodes_text", "").strip() and not cleaned.get("timecodes_file") and not has_existing_file:
            raise forms.ValidationError(_("Enter text with timecodes or upload a TXT file."))
        text = cleaned.get("timecodes_text", "").strip()
        uploaded = cleaned.get("timecodes_file")
        filename = ""
        if not text and uploaded:
            filename = uploaded.name
            try:
                text = uploaded.read().decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise forms.ValidationError(_("The script file must use UTF-8 encoding.")) from error
            finally:
                uploaded.seek(0)
        if text:
            try:
                parse_script(text, filename=filename)
            except SubtitleParseError as error:
                raise forms.ValidationError(_("The script could not be parsed: %(error)s") % {"error": error}) from error
        return cleaned


class VoiceoverAudioForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["audio_file"]
        labels = {"audio_file": _("Voiceover recording")}
        widgets = {
            "audio_file": forms.FileInput(attrs={"accept": "audio/*,video/mp4,video/webm,.mkv"}),
        }

    def clean_audio_file(self):
        return validate_extension(
            self.cleaned_data["audio_file"], ALLOWED_AUDIO,
            _("Unsupported audio format. Allowed formats: %(formats)s.") % {"formats": AUDIO_EXTENSIONS},
        )


class ProjectTimecodesForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["timecodes_text", "timecodes_file"]
        labels = {
            "timecodes_text": _("Text with video timecodes"),
            "timecodes_file": _("Or upload TXT, SRT, VTT or CSV"),
        }
        widgets = {
            "timecodes_text": forms.Textarea(attrs={"rows": 12, "placeholder": "00:00 - First phrase\n00:08 - Second phrase"}),
            "timecodes_file": forms.FileInput(attrs={"accept": ".txt,.srt,.vtt,.csv,text/plain,text/vtt,text/csv"}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["timecodes_text"].required = False
        self.fields["timecodes_file"].required = False

    def clean_timecodes_file(self):
        upload = self.cleaned_data.get("timecodes_file")
        if upload and Path(upload.name).suffix.lower() not in ALLOWED_SCRIPTS:
            raise forms.ValidationError(_("Unsupported script format. Upload TXT, SRT, VTT or CSV."))
        return upload

    def clean(self):
        cleaned = super().clean()
        text = cleaned.get("timecodes_text", "").strip()
        upload = cleaned.get("timecodes_file")
        existing = bool(self.instance.pk and self.instance.timecodes_file)
        if not text and not upload and not existing:
            raise forms.ValidationError(_("Enter text with timecodes or upload a TXT, SRT, VTT or CSV file."))
        filename = ""
        if not text and upload:
            filename = upload.name
            try:
                text = upload.read().decode("utf-8-sig")
            except UnicodeDecodeError as error:
                raise forms.ValidationError(_("The script file must use UTF-8 encoding.")) from error
            finally:
                upload.seek(0)
        if text:
            try:
                parse_script(text, filename=filename)
            except SubtitleParseError as error:
                raise forms.ValidationError(_("The script could not be parsed: %(error)s") % {"error": error}) from error
        return cleaned

class VideoUploadForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = [
            "video_file", "output_mode", "export_format",
            "normalize_loudness", "speed_warning_threshold", "retention_days",
        ]
        labels = {
            "video_file": _("Video"),
            "output_mode": _("Export mode"),
            "export_format": _("Audio format"),
            "normalize_loudness": _("Normalize phrase loudness"),
            "speed_warning_threshold": _("Warn about speed-up above"),
            "retention_days": _("Keep project files"),
        }
        help_texts = {
            "normalize_loudness": _("Makes phrases sound consistently loud in the final track."),
            "speed_warning_threshold": _("For example, 1.12 means a 12%% speed-up."),
            "retention_days": _("Expired local projects and their files are removed automatically."),
        }
        widgets = {"video_file": forms.FileInput(attrs={"accept": "video/*,.mkv"})}

    def clean_speed_warning_threshold(self):
        value = self.cleaned_data["speed_warning_threshold"]
        if not 1.0 <= value <= 3.0:
            raise forms.ValidationError(_("Choose a value between 1.00 and 3.00."))
        return value

    def clean_video_file(self):
        file = self.cleaned_data["video_file"]
        extension = Path(file.name).suffix.lower()
        if extension not in ALLOWED_VIDEO:
            raise forms.ValidationError(_("Unsupported video format. Allowed formats: %(formats)s.") % {"formats": VIDEO_EXTENSIONS})
        return file


class VideoAudioMergeForm(forms.ModelForm):
    class Meta:
        model = VideoAudioMergeJob
        fields = [
            "video_file", "audio_file", "mix_original", "original_volume",
            "voice_volume", "duck_original", "start_offset", "trim_to_video",
        ]
        labels = {
            "video_file": _("Source video"),
            "audio_file": _("New audio track"),
            "mix_original": _("Mix with the original sound"),
            "original_volume": _("Original sound volume"),
            "voice_volume": _("Voiceover volume"),
            "duck_original": _("Automatically lower the original sound during speech"),
            "start_offset": _("Voiceover start offset, seconds"),
            "trim_to_video": _("Trim the result to the video duration"),
        }
        help_texts = {
            "mix_original": _(
                "When enabled, both tracks will play together. "
                "When disabled, the original video sound will be muted."
            ),
        }
        widgets = {
            "video_file": forms.FileInput(
                attrs={"accept": "video/*,.mkv,.m4v"}
            ),
            "audio_file": forms.FileInput(
                attrs={
                    "accept": (
                        "audio/*,video/mp4,video/webm,.mkv,.m4a,.aac,.flac"
                    )
                }
            ),
            "mix_original": forms.CheckboxInput(),
            "duck_original": forms.CheckboxInput(),
            "trim_to_video": forms.CheckboxInput(),
            "original_volume": forms.NumberInput(attrs={"min": 0, "max": 2, "step": .05}),
            "voice_volume": forms.NumberInput(attrs={"min": 0, "max": 2, "step": .05}),
            "start_offset": forms.NumberInput(attrs={"min": 0, "step": .1}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        defaults = {"original_volume": .35, "voice_volume": 1.0, "start_offset": 0.0}
        for name, value in defaults.items():
            self.fields[name].required = False
            self.fields[name].initial = value

    def clean_video_file(self):
        file = self.cleaned_data["video_file"]
        if Path(file.name).suffix.lower() not in ALLOWED_VIDEO:
            raise forms.ValidationError(
                _("Unsupported video format. Allowed formats: %(formats)s.")
                % {"formats": VIDEO_EXTENSIONS}
            )
        return file

    def clean_audio_file(self):
        file = self.cleaned_data["audio_file"]
        if Path(file.name).suffix.lower() not in ALLOWED_AUDIO:
            raise forms.ValidationError(
                _("Unsupported audio format. Allowed formats: %(formats)s.")
                % {"formats": AUDIO_EXTENSIONS}
            )
        return file

    def clean(self):
        cleaned = super().clean()
        for name, default in (("original_volume", .35), ("voice_volume", 1.0), ("start_offset", 0.0)):
            if cleaned.get(name) is None:
                cleaned[name] = default
        for name in ("original_volume", "voice_volume"):
            value = cleaned.get(name)
            if value is not None and not 0 <= value <= 2:
                self.add_error(name, _("Choose a value between 0 and 2."))
        if cleaned.get("duck_original") and not cleaned.get("mix_original"):
            self.add_error("duck_original", _("Enable mixing with the original sound to use automatic ducking."))
        return cleaned


class LocalizationProjectForm(forms.ModelForm):
    languages = forms.MultipleChoiceField(
        choices=LANGUAGE_CHOICES,
        widget=forms.CheckboxSelectMultiple,
        label=_("Target languages"),
    )

    class Meta:
        model = LocalizationProject
        fields = ["title", "source_video", "source_script", "source_script_text", "languages", "copy_settings"]
        labels = {
            "title": _("Project name"), "source_video": _("Source video"),
            "source_script": _("Source script"), "source_script_text": _("Or paste the source script"),
            "copy_settings": _("Copy timing and export settings to every language"),
        }
        widgets = {
            "source_video": forms.FileInput(attrs={"accept": "video/*,.mkv"}),
            "source_script": forms.FileInput(attrs={"accept": ".txt,.srt,.vtt,.csv"}),
            "source_script_text": forms.Textarea(attrs={"rows": 7}),
        }

    def clean_source_video(self):
        return validate_extension(self.cleaned_data["source_video"], ALLOWED_VIDEO, _("Unsupported video format."))

    def clean_source_script(self):
        return validate_extension(self.cleaned_data.get("source_script"), ALLOWED_SCRIPTS, _("Unsupported script format."))

    def clean(self):
        cleaned = super().clean()
        upload, text = cleaned.get("source_script"), cleaned.get("source_script_text", "").strip()
        if not upload and not text:
            raise forms.ValidationError(_("Upload or paste the source script."))
        if upload and not text:
            try:
                text = upload.read().decode("utf-8-sig")
                upload.seek(0)
            except UnicodeDecodeError as error:
                raise forms.ValidationError(_("The script file must use UTF-8 encoding.")) from error
        if text:
            try:
                parse_script(text, upload.name if upload else "")
            except SubtitleParseError as error:
                raise forms.ValidationError(_("The script could not be parsed: %(error)s") % {"error": error}) from error
        return cleaned


class LocalizationVersionForm(forms.Form):
    script_file = forms.FileField(required=False, widget=forms.FileInput(attrs={"accept": ".txt,.srt,.vtt,.csv"}), label=_("Translated script"))
    script_text = forms.CharField(required=False, widget=forms.Textarea(attrs={"rows": 6}), label=_("Or paste the translated script"))
    audio_file = forms.FileField(widget=forms.FileInput(attrs={"accept": "audio/*,video/mp4,video/webm,.mkv"}), label=_("Voiceover recording"))

    def clean_script_file(self):
        return validate_extension(self.cleaned_data.get("script_file"), ALLOWED_SCRIPTS, _("Unsupported script format."))

    def clean_audio_file(self):
        return validate_extension(self.cleaned_data["audio_file"], ALLOWED_AUDIO, _("Unsupported audio format."))

    def clean(self):
        cleaned = super().clean()
        if not cleaned.get("script_file") and not cleaned.get("script_text", "").strip():
            raise forms.ValidationError(_("Add the translated script for this language."))
        return cleaned


class TakesProjectForm(forms.ModelForm):
    recordings = MultipleFileField(label=_("Recordings with takes"))

    class Meta:
        model = TakesProject
        fields = ["title", "video_file", "script_file", "script_text", "mode"]
        labels = {
            "title": _("Project name"), "video_file": _("Target video"),
            "script_file": _("Script with target timecodes"), "script_text": _("Or paste the script"),
            "mode": _("How were the takes recorded?"),
        }
        widgets = {
            "video_file": forms.FileInput(attrs={"accept": "video/*,.mkv"}),
            "script_file": forms.FileInput(attrs={"accept": ".txt,.srt,.vtt,.csv"}),
            "script_text": forms.Textarea(attrs={"rows": 7}),
            "mode": forms.RadioSelect,
        }

    def clean_video_file(self):
        return validate_extension(self.cleaned_data["video_file"], ALLOWED_VIDEO, _("Unsupported video format."))

    def clean_recordings(self):
        files = self.cleaned_data["recordings"]
        for upload in files:
            validate_extension(upload, ALLOWED_AUDIO, _("Unsupported recording format."))
        return files

    def clean(self):
        cleaned = super().clean()
        upload, text = cleaned.get("script_file"), cleaned.get("script_text", "").strip()
        if not upload and not text:
            raise forms.ValidationError(_("Upload or paste the script with target timecodes."))
        if upload and not text:
            try:
                text = upload.read().decode("utf-8-sig")
                upload.seek(0)
            except UnicodeDecodeError as error:
                raise forms.ValidationError(_("The script file must use UTF-8 encoding.")) from error
        if text:
            try:
                parse_script(text, upload.name if upload else "")
            except SubtitleParseError as error:
                raise forms.ValidationError(_("The script could not be parsed: %(error)s") % {"error": error}) from error
        return cleaned


class ReplaceFragmentToolForm(forms.Form):
    source = forms.FileField(label=_("Finished video or audio"))
    replacement = forms.FileField(label=_("New phrase"))
    start = forms.FloatField(min_value=0, label=_("Interval start, seconds"))
    end = forms.FloatField(min_value=0.01, label=_("Interval end, seconds"))
    keep_duration = forms.BooleanField(required=False, initial=True, label=_("Keep the original interval duration"))
    transition_ms = forms.ChoiceField(choices=[("20", "20 ms"), ("40", "40 ms"), ("80", "80 ms"), ("120", "120 ms")], initial="40", label=_("Transition"))
    volume = forms.FloatField(min_value=0, max_value=2, initial=1, label=_("New phrase volume"))

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("start") is not None and cleaned.get("end") is not None and cleaned["end"] <= cleaned["start"]:
            raise forms.ValidationError(_("The interval end must be after its start."))
        return cleaned


class AudioEditorToolForm(Mp3QualityFormMixin, forms.Form):
    audio = MultipleFileField(
        label=_("Audio files"),
        widget=MultipleFileInput(attrs={"accept": "audio/*,video/mp4,video/webm,.mkv"}),
    )
    output_format = forms.ChoiceField(
        choices=[("mp3", _("MP3 — compact")), ("wav", _("WAV — lossless"))],
        initial="mp3",
        label=_("Output format"),
    )

    def clean_audio(self):
        return [
            validate_extension(
                upload, ALLOWED_AUDIO,
                _("Unsupported audio format. Allowed formats: %(formats)s.") % {"formats": AUDIO_EXTENSIONS},
            )
            for upload in self.cleaned_data["audio"]
        ]


class SplitPhrasesToolForm(Mp3QualityFormMixin, forms.Form):
    audio = forms.FileField(label=_("Long voiceover recording"))
    script = forms.FileField(
        required=False,
        label=_("Text file"),
        help_text=_("TXT in UTF-8. Each non-empty line will become a separate phrase."),
        widget=forms.FileInput(attrs={"accept": ".txt,text/plain"}),
    )
    script_text = forms.CharField(
        required=False,
        label=_("Voiceover text"),
        help_text=_("Start each phrase on a new line. Empty lines and lines containing only spaces are ignored."),
        widget=forms.Textarea(attrs={
            "rows": 12,
            "placeholder": _("First phrase.\nSecond phrase.\nThird phrase."),
        }),
    )
    output_format = forms.ChoiceField(
        choices=[("wav", _("WAV — lossless")), ("mp3", _("MP3 — compact"))],
        initial="wav",
        label=_("Output format"),
    )
    def clean(self):
        cleaned = super().clean()
        upload, text = cleaned.get("script"), cleaned.get("script_text", "").strip()
        if upload and Path(upload.name).suffix.lower() != ".txt":
            self.add_error("script", _("Upload a TXT file in UTF-8."))
        if upload and not text:
            try:
                text = upload.read().decode("utf-8-sig")
                upload.seek(0)
            except UnicodeDecodeError:
                self.add_error("script", _("The text file must use UTF-8 encoding."))
        if not upload and not text:
            raise forms.ValidationError(_("Upload a text file or paste the voiceover text."))
        phrases = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()] if text else []
        if text and not any(phrases):
            raise forms.ValidationError(_("The text contains no phrases."))
        cleaned["script_text"] = text
        return cleaned


class FitSubtitlesToolForm(forms.Form):
    subtitles = forms.FileField(label=_("Subtitles with timecodes"))
    voiceovers = MultipleFileField(label=_("Voiceover"))
    speed_limit = forms.ChoiceField(choices=[("1.12", _("Up to 12%")), ("1.20", _("Up to 20%")), ("3.0", _("Fit automatically"))], initial="1.12", label=_("Allowed speed-up"))


class PrepareVoiceoverToolForm(Mp3QualityFormMixin, forms.Form):
    files = MultipleFileField(label=_("Voiceover files"))
    trim_edges = forms.BooleanField(required=False, initial=True, label=_("Trim silence at the edges"))
    normalize = forms.BooleanField(required=False, initial=True, label=_("Even out loudness"))
    prevent_clipping = forms.BooleanField(required=False, initial=True, label=_("Prevent clipping"))
    soft_edges = forms.BooleanField(required=False, initial=True, label=_("Add soft edges"))
    output_format = forms.ChoiceField(choices=[("wav", "WAV"), ("mp3", "MP3")], label=_("Output format"))
    loudness_target = forms.ChoiceField(choices=[("-16", "-16 LUFS"), ("-14", "-14 LUFS"), ("-18", "-18 LUFS")], initial="-16", label=_("Loudness target"))


class CheckDurationsToolForm(forms.Form):
    subtitles = forms.FileField(label=_("Subtitles"))
    phrase_files = MultipleFileField(label=_("Phrase files"))
    warning_threshold = forms.FloatField(min_value=1, max_value=3, initial=1.12, label=_("Warning threshold"))


class CreateSubtitlesToolForm(forms.Form):
    script = forms.FileField(label=_("Prepared script"))
    voiceover = forms.FileField(label=_("Finished voiceover"))
    subtitle_format = forms.ChoiceField(choices=[("srt", "SRT"), ("vtt", "VTT")], label=_("Subtitle format"))


class TranscribeAudioToolForm(forms.Form):
    media = forms.FileField(
        label=_("Audio or video"),
        widget=forms.FileInput(attrs={"accept": "audio/*,video/*,.mkv,.flac,.m4a,.ogg,.webm"}),
    )
    language = forms.ChoiceField(
        choices=[
            ("", _("Detect automatically")),
            ("ru", _("Russian")),
            ("en", _("English")),
            ("es", _("Spanish")),
            ("de", _("German")),
        ],
        required=False,
        label=_("Speech language"),
    )
    export_format = forms.ChoiceField(
        choices=[("txt", "TXT"), ("docx", "DOCX"), ("both", "TXT + DOCX")],
        initial="txt",
        label=_("Export format"),
    )
    transcription_mode = forms.ChoiceField(
        choices=[("accurate", _("Accurate transcript")), ("readable", _("Readable text"))],
        initial="readable",
        widget=forms.RadioSelect,
        label=_("Text formatting"),
    )
    terms = forms.CharField(
        required=False,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": _("For example: FormaCode, Pulse, FFmpeg, SRT")}),
        label=_("Names and special terms"),
    )
    divide_paragraphs = forms.BooleanField(required=False, initial=True, label=_("Divide into paragraphs"))
    preserve_wording = forms.BooleanField(required=False, initial=True, label=_("Preserve speaker wording"))

    def clean_media(self):
        allowed = ALLOWED_AUDIO | ALLOWED_VIDEO
        return validate_extension(self.cleaned_data["media"], allowed, _("Unsupported audio or video format."))


class PhraseTimingToolForm(forms.ModelForm):
    media_file = forms.FileField(
        label=_("Video or audio"),
        widget=forms.FileInput(attrs={"accept": "audio/*,video/*,.mkv,.flac,.m4a,.ogg,.webm"}),
    )
    source_text = forms.CharField(
        required=False,
        label=_("Voiceover text"),
        widget=forms.Textarea(attrs={
            "rows": 12,
            "placeholder": _("Paste the text that should be spoken in the video."),
        }),
    )

    class Meta:
        model = PhraseTimingProject
        fields = ["mode", "media_file", "source_text"]
        labels = {
            "mode": _("How should timecodes be determined?"),
            "media_file": _("Video or audio"),
            "source_text": _("Voiceover text"),
        }
        widgets = {
            "mode": forms.RadioSelect,
            "source_text": forms.Textarea(attrs={
                "rows": 12,
                "placeholder": _("Paste the text that should be spoken in the video."),
            }),
        }

    def __init__(self, *args, media_override=None, **kwargs):
        self.media_override = media_override
        if media_override is None and len(args) > 1 and args[1] and "video_file" in args[1] and "media_file" not in args[1]:
            args = list(args)
            files = args[1].copy()
            files["media_file"] = files["video_file"]
            args[1] = files
        super().__init__(*args, **kwargs)
        if media_override is not None:
            self.fields.pop("media_file")

    def clean_media_file(self):
        return validate_extension(
            self.cleaned_data["media_file"], ALLOWED_AUDIO | ALLOWED_VIDEO,
            _("Unsupported audio or video format."),
        )

    def clean_source_text(self):
        value = self.cleaned_data.get("source_text", "").strip()
        if self.cleaned_data.get("mode") == PhraseTimingProject.MODE_MANUAL and not value:
            raise forms.ValidationError(_("Add the voiceover text."))
        return value if self.cleaned_data.get("mode") == PhraseTimingProject.MODE_MANUAL else ""

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("mode") != PhraseTimingProject.MODE_AUTOMATIC:
            return cleaned
        media = self.media_override or cleaned.get("media_file")
        if not media:
            return cleaned
        temporary_path = None
        try:
            if hasattr(media, "temporary_file_path"):
                path = media.temporary_file_path()
            elif self.media_override is not None:
                path = media.path
            else:
                suffix = Path(media.name).suffix
                with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as temporary:
                    for chunk in media.chunks():
                        temporary.write(chunk)
                    temporary_path = Path(temporary.name)
                path = temporary_path
            if not probe_has_audio(path):
                message = _("The selected file has no audio track. Automatic timing requires audio with speech.")
                if "media_file" in self.fields:
                    self.add_error("media_file", message)
                else:
                    raise forms.ValidationError(message)
        except ProcessingError as error:
            if "media_file" in self.fields:
                self.add_error("media_file", _("The media file could not be read: %(error)s") % {"error": error})
            else:
                raise forms.ValidationError(
                    _("The media file could not be read: %(error)s") % {"error": error}
                ) from error
        finally:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)
            if hasattr(media, "seek"):
                media.seek(0)
        return cleaned

    def save(self, commit=True):
        project = super().save(commit=False)
        media = self.media_override or self.cleaned_data.get("media_file")
        if media is not None:
            project.video_file = media.name if self.media_override is not None else media
        if commit:
            project.save()
        return project


TOOL_FORMS = {
    "edit-audio": AudioEditorToolForm,
    "replace-fragment": ReplaceFragmentToolForm,
    "split-phrases": SplitPhrasesToolForm,
    "fit-subtitles": FitSubtitlesToolForm,
    "prepare-voiceover": PrepareVoiceoverToolForm,
    "check-durations": CheckDurationsToolForm,
    "create-subtitles": CreateSubtitlesToolForm,
}
