from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("studio", "0014_phrasetimingproject")]

    operations = [
        migrations.AlterField(
            model_name="phrasetimingproject",
            name="source_text",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="phrasetimingproject",
            name="review_items",
            field=models.JSONField(blank=True, default=list),
        ),
        migrations.AddField(
            model_name="phrasetimingproject",
            name="processing_status",
            field=models.CharField(default="complete", max_length=20),
        ),
        migrations.AddField(
            model_name="phrasetimingproject",
            name="processing_step",
            field=models.CharField(default="complete", max_length=40),
        ),
        migrations.AddField(
            model_name="phrasetimingproject",
            name="processing_error",
            field=models.TextField(blank=True),
        ),
    ]
