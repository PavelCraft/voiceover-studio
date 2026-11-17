from django.db import migrations, models

import studio.models


class Migration(migrations.Migration):
    dependencies = [("studio", "0019_storagereservation")]

    operations = [
        migrations.AlterField(
            model_name="localizationproject",
            name="source_script",
            field=models.FileField(blank=True, max_length=500, null=True, upload_to=studio.models.localization_upload_to),
        ),
        migrations.AlterField(
            model_name="localizationproject",
            name="source_video",
            field=models.FileField(max_length=500, upload_to=studio.models.localization_upload_to),
        ),
        migrations.AlterField(
            model_name="localizationversion",
            name="audio_file",
            field=models.FileField(blank=True, max_length=500, null=True, upload_to=studio.models.localization_version_upload_to),
        ),
        migrations.AlterField(
            model_name="localizationversion",
            name="script_file",
            field=models.FileField(blank=True, max_length=500, null=True, upload_to=studio.models.localization_version_upload_to),
        ),
        migrations.AlterField(
            model_name="mediatoolinput",
            name="media_file",
            field=models.FileField(max_length=500, upload_to=studio.models.media_tool_input_upload_to),
        ),
        migrations.AlterField(
            model_name="phrasetimingproject",
            name="video_file",
            field=models.FileField(max_length=500, upload_to=studio.models.phrase_timing_video_upload_to),
        ),
        migrations.AlterField(
            model_name="project",
            name="audio_file",
            field=models.FileField(blank=True, max_length=500, null=True, upload_to=studio.models.audio_upload_to),
        ),
        migrations.AlterField(
            model_name="project",
            name="timecodes_file",
            field=models.FileField(blank=True, max_length=500, null=True, upload_to=studio.models.timecodes_upload_to),
        ),
        migrations.AlterField(
            model_name="project",
            name="video_file",
            field=models.FileField(blank=True, max_length=500, null=True, upload_to=studio.models.video_upload_to),
        ),
        migrations.AlterField(
            model_name="segment",
            name="audio_file",
            field=models.FileField(blank=True, max_length=500, upload_to=studio.models.segment_upload_to),
        ),
        migrations.AlterField(
            model_name="segmentreplacement",
            name="audio_file",
            field=models.FileField(max_length=500, upload_to=studio.models.replacement_audio_upload_to),
        ),
        migrations.AlterField(
            model_name="takecandidate",
            name="audio_file",
            field=models.FileField(blank=True, max_length=500, upload_to=studio.models.take_candidate_upload_to),
        ),
        migrations.AlterField(
            model_name="takerecording",
            name="media_file",
            field=models.FileField(max_length=500, upload_to=studio.models.take_recording_upload_to),
        ),
        migrations.AlterField(
            model_name="takesproject",
            name="script_file",
            field=models.FileField(blank=True, max_length=500, null=True, upload_to=studio.models.takes_upload_to),
        ),
        migrations.AlterField(
            model_name="takesproject",
            name="video_file",
            field=models.FileField(max_length=500, upload_to=studio.models.takes_upload_to),
        ),
        migrations.AlterField(
            model_name="videoaudiomergejob",
            name="audio_file",
            field=models.FileField(max_length=500, upload_to=studio.models.merge_audio_upload_to),
        ),
        migrations.AlterField(
            model_name="videoaudiomergejob",
            name="video_file",
            field=models.FileField(max_length=500, upload_to=studio.models.merge_video_upload_to),
        ),
    ]
