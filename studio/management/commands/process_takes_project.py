from django.core.management.base import BaseCommand, CommandError

from studio.models import TakesProject
from studio.services.takes import process_takes_project
from studio.services.storage import release_storage


class Command(BaseCommand):
    help = "Find and cut takes for one project"

    def add_arguments(self, parser):
        parser.add_argument("project_id")

    def handle(self, *args, **options):
        try:
            project = TakesProject.objects.get(pk=options["project_id"])
        except TakesProject.DoesNotExist as error:
            raise CommandError("Takes project not found") from error
        try:
            process_takes_project(project)
        finally:
            release_storage(f"takes:{project.pk}")
