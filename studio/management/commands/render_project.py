from django.core.management.base import BaseCommand, CommandError

from studio.models import Project
from studio.services.voiceover import render_project_result


class Command(BaseCommand):
    help = "Render the approved voiceover fragments into the final result."

    def add_arguments(self, parser):
        parser.add_argument("project_id")

    def handle(self, *args, **options):
        try:
            project = Project.objects.get(pk=options["project_id"])
        except (Project.DoesNotExist, ValueError) as error:
            raise CommandError("Project not found") from error
        render_project_result(project)

