from django.db import migrations, models
import django.db.models.deletion
import studio.models


class Migration(migrations.Migration):
    dependencies = [("studio", "0001_initial")]

    operations = [
        migrations.AddField(model_name="project", name="timecodes_file", field=models.FileField(blank=True, null=True, upload_to=studio.models.timecodes_upload_to)),
        migrations.AddField(model_name="project", name="processing_status", field=models.CharField(default="pending", max_length=20)),
        migrations.AddField(model_name="project", name="processing_error", field=models.TextField(blank=True)),
        migrations.AddField(model_name="project", name="transcription_file", field=models.CharField(blank=True, max_length=500)),
        migrations.AddField(model_name="project", name="output_file", field=models.CharField(blank=True, max_length=500)),
        migrations.CreateModel(
            name="Segment",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveIntegerField()),
                ("text", models.TextField()),
                ("video_start", models.FloatField()),
                ("video_end", models.FloatField()),
                ("audio_start", models.FloatField(blank=True, null=True)),
                ("audio_end", models.FloatField(blank=True, null=True)),
                ("recognition_score", models.FloatField(blank=True, null=True)),
                ("status", models.CharField(default="pending", max_length=30)),
                ("audio_file", models.FileField(blank=True, upload_to=studio.models.segment_upload_to)),
                ("source_duration", models.FloatField(blank=True, null=True)),
                ("output_duration", models.FloatField(blank=True, null=True)),
                ("speed", models.FloatField(default=1.0)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="segments", to="studio.project")),
            ],
            options={"ordering": ["position"]},
        ),
        migrations.AddConstraint(model_name="segment", constraint=models.UniqueConstraint(fields=("project", "position"), name="unique_project_segment_position")),
    ]
