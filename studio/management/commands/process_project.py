from django.core.management.base import BaseCommand, CommandError

from studio.models import Project
from studio.services.voiceover import process_project


class Command(BaseCommand):
    help = "Process one voiceover project in an independent worker process."

    def add_arguments(self, parser):
        parser.add_argument("project_id")
        parser.add_argument("--reuse-transcription", action="store_true")
        parser.add_argument("--realign-from", type=int)

    def handle(self, *args, **options):
        try:
            project = Project.objects.get(pk=options["project_id"])
        except (Project.DoesNotExist, ValueError) as error:
            raise CommandError("Project not found") from error
        process_project(
            project,
            reuse_transcription=options["reuse_transcription"],
            realign_from_position=options["realign_from"],
        )
