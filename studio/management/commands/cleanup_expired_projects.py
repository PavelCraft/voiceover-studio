from django.core.management.base import BaseCommand
from studio.services.retention import purge_expired_projects


class Command(BaseCommand):
    help = "Delete projects whose configured local retention period has expired."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true")

    def handle(self, *args, **options):
        expired = purge_expired_projects(dry_run=options["dry_run"])
        for project_id in expired:
            self.stdout.write(project_id)
        self.stdout.write(f"Expired projects: {len(expired)}")
