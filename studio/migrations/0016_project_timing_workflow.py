from django.db import migrations, models
import django.db.models.deletion
import studio.models


class Migration(migrations.Migration):
    dependencies = [("studio", "0015_phrasetimingproject_automatic_processing")]

    operations = [
        migrations.AlterField(
            model_name="project",
            name="audio_file",
            field=models.FileField(blank=True, null=True, upload_to=studio.models.audio_upload_to),
        ),
        migrations.AddField(
            model_name="project",
            name="timing_project",
            field=models.OneToOneField(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="voiceover_project",
                to="studio.phrasetimingproject",
            ),
        ),
    ]
