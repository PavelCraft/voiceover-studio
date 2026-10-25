import uuid

from django.db import migrations, models
from django.db.models import F


def connect_existing_transcriptions(apps, schema_editor):
    Project = apps.get_model("studio", "Project")
    Project.objects.exclude(transcription_file="").update(
        transcription_audio_revision=F("audio_revision")
    )


class Migration(migrations.Migration):
    dependencies = [("studio", "0017_project_pending_reprocessing")]

    operations = [
        migrations.AddField(
            model_name="project",
            name="audio_revision",
            field=models.UUIDField(default=uuid.uuid4, editable=False),
        ),
        migrations.AddField(
            model_name="project",
            name="transcription_audio_revision",
            field=models.UUIDField(blank=True, editable=False, null=True),
        ),
        migrations.RunPython(connect_existing_transcriptions, migrations.RunPython.noop),
    ]
