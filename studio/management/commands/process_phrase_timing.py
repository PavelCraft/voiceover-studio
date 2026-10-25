from django.core.management.base import BaseCommand, CommandError

from studio.models import PhraseTimingProject
from studio.services.phrase_timing import process_phrase_timing_project
from studio.services.storage import release_storage


class Command(BaseCommand):
    help = "Automatically recognize phrases and their timecodes"

    def add_arguments(self, parser):
        parser.add_argument("project_id")

    def handle(self, *args, **options):
        try:
            project = PhraseTimingProject.objects.get(pk=options["project_id"])
        except PhraseTimingProject.DoesNotExist as error:
            raise CommandError("Phrase timing project not found") from error
        try:
            process_phrase_timing_project(project)
        finally:
            release_storage(f"phrase-timing:{project.pk}")
