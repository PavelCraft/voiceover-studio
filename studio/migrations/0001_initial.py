from django.db import migrations, models
import django.db.models.deletion
import uuid
import studio.models

class Migration(migrations.Migration):
    initial = True
    dependencies = []
    operations = [migrations.CreateModel(name="Project", fields=[
        ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
        ("audio_file", models.FileField(upload_to=studio.models.audio_upload_to)),
        ("video_file", models.FileField(blank=True, null=True, upload_to=studio.models.video_upload_to)),
        ("output_mode", models.CharField(choices=[("video", "Video with the voiceover"), ("audio", "Audio track for YouTube")], default="audio", max_length=20)),
        ("created_at", models.DateTimeField(auto_now_add=True)),
    ])]
