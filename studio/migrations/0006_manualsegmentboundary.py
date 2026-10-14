from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [("studio", "0005_videoaudiomergejob")]

    operations = [
        migrations.CreateModel(
            name="ManualSegmentBoundary",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("position", models.PositiveIntegerField()),
                ("audio_start", models.FloatField(blank=True, null=True)),
                ("audio_end", models.FloatField(blank=True, null=True)),
                ("skipped", models.BooleanField(default=False)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="manual_boundaries", to="studio.project")),
            ],
            options={"ordering": ["position"]},
        ),
        migrations.AddConstraint(
            model_name="manualsegmentboundary",
            constraint=models.UniqueConstraint(fields=("project", "position"), name="unique_manual_boundary_position"),
        ),
    ]
