from django.core.management.base import BaseCommand, CommandError

from studio.models import VideoAudioMergeJob
from studio.services.media_tools import merge_video_with_audio


class Command(BaseCommand):
    help = "Merge video and audio in an independent worker process."

    def add_arguments(self, parser):
        parser.add_argument("job_id")

    def handle(self, *args, **options):
        try:
            job = VideoAudioMergeJob.objects.get(pk=options["job_id"])
        except (VideoAudioMergeJob.DoesNotExist, ValueError) as error:
            raise CommandError("Merge job not found") from error
        merge_video_with_audio(job)
