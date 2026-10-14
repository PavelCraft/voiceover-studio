from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ("studio", "0006_manualsegmentboundary"),
    ]

    operations = [
        migrations.AddField(
            model_name="project",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="voiceover_projects",
                to=settings.AUTH_USER_MODEL,
            ),
        ),
    ]
