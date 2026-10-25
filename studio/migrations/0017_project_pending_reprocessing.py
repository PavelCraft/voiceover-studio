from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0016_project_timing_workflow")]

    operations = [
        migrations.AddField(
            model_name="project",
            name="pending_reprocessing",
            field=models.JSONField(blank=True, default=dict),
        ),
    ]
