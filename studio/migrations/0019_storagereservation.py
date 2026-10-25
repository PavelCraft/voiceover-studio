from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0018_project_audio_revisions")]

    operations = [
        migrations.CreateModel(
            name="StorageReservation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("key", models.CharField(max_length=120, unique=True)),
                ("reserved_bytes", models.PositiveBigIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ["created_at"]},
        ),
    ]
