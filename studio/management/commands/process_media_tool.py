from django.core.management.base import BaseCommand, CommandError

from studio.models import MediaToolJob
from studio.services.tool_processing import process_media_tool_job
from studio.services.storage import release_storage


class Command(BaseCommand):
    help = "Process one media tool job"

    def add_arguments(self, parser):
        parser.add_argument("job_id")

    def handle(self, *args, **options):
        try:
            job = MediaToolJob.objects.get(pk=options["job_id"])
        except MediaToolJob.DoesNotExist as error:
            raise CommandError("Media tool job not found") from error
        try:
            process_media_tool_job(job)
        finally:
            release_storage(f"media-tool:{job.pk}")
