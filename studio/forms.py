from pathlib import Path
from django import forms
from django.utils.translation import gettext_lazy as _
from .models import Project, VideoAudioMergeJob

AUDIO_EXTENSIONS = ".mp3, .wav, .webm, .ogg, .m4a, .aac, .flac, .mp4, .mkv"
VIDEO_EXTENSIONS = ".mp4, .webm, .mkv, .mov, .avi, .m4v"
ALLOWED_AUDIO = {".mp3", ".wav", ".webm", ".ogg", ".m4a", ".aac", ".flac", ".mp4", ".mkv"}
ALLOWED_VIDEO = {".mp4", ".webm", ".mkv", ".mov", ".avi", ".m4v"}

class AudioUploadForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["audio_file", "timecodes_text", "timecodes_file"]
        labels = {
            "audio_file": _("Voiceover recording"),
            "timecodes_text": _("Text with video timecodes"),
            "timecodes_file": _("Or upload a TXT file"),
        }
        widgets = {
            "audio_file": forms.FileInput(attrs={"accept": "audio/*,video/mp4,video/webm,.mkv"}),
            "timecodes_text": forms.Textarea(attrs={"rows": 12, "placeholder": "00:00 - First phrase\n00:08 - Second phrase"}),
            "timecodes_file": forms.FileInput(attrs={"accept": ".txt,text/plain"}),
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
        if file and Path(file.name).suffix.lower() != ".txt":
            raise forms.ValidationError(_("Unsupported timecode format. Upload a TXT file."))
        return file

    def clean(self):
        cleaned = super().clean()
        has_existing_file = bool(self.instance.pk and self.instance.timecodes_file)
        if not cleaned.get("timecodes_text", "").strip() and not cleaned.get("timecodes_file") and not has_existing_file:
            raise forms.ValidationError(_("Enter text with timecodes or upload a TXT file."))
        return cleaned

class VideoUploadForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["video_file", "output_mode"]
        labels = {"video_file": _("Video"), "output_mode": _("Export mode")}
        widgets = {"video_file": forms.FileInput(attrs={"accept": "video/*,.mkv"})}

    def clean_video_file(self):
        file = self.cleaned_data["video_file"]
        extension = Path(file.name).suffix.lower()
        if extension not in ALLOWED_VIDEO:
            raise forms.ValidationError(_("Unsupported video format. Allowed formats: %(formats)s.") % {"formats": VIDEO_EXTENSIONS})
        return file


class VideoAudioMergeForm(forms.ModelForm):
    class Meta:
        model = VideoAudioMergeJob
        fields = ["video_file", "audio_file", "mix_original"]
        labels = {
            "video_file": _("Source video"),
            "audio_file": _("New audio track"),
            "mix_original": _("Mix with the original sound"),
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
        }

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
