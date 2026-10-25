import uuid
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
import studio.models


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("studio", "0013_alter_mediatooljob_kind"),
    ]

    operations = [
        migrations.CreateModel(
            name="PhraseTimingProject",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("video_file", models.FileField(upload_to=studio.models.phrase_timing_video_upload_to)),
                ("source_text", models.TextField()),
                ("mode", models.CharField(choices=[("manual", "Manual phrase timing"), ("automatic", "Automatic phrase timing")], default="manual", max_length=20)),
                ("phrases", models.JSONField(default=list)),
                ("video_duration", models.FloatField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("owner", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="phrase_timing_projects", to=settings.AUTH_USER_MODEL)),
            ],
        ),
    ]
