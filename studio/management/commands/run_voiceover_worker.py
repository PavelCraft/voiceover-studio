import time

from django.core.management.base import BaseCommand

from studio.services.job_queue import claim_job, execute_job, recover_stale_jobs
from studio.services.retention import purge_expired_projects
from studio.services.storage import purge_stale_reservations


class Command(BaseCommand):
    help = "Run durable voiceover jobs. Use without --once for a persistent worker."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--job")
        parser.add_argument("--poll", type=float, default=1.0)

    def handle(self, *args, **options):
        purge_expired_projects()
        purge_stale_reservations()
        next_cleanup = time.monotonic() + 24 * 60 * 60
        recover_stale_jobs()
        while True:
            if time.monotonic() >= next_cleanup:
                purge_expired_projects()
                purge_stale_reservations()
                recover_stale_jobs()
                next_cleanup = time.monotonic() + 24 * 60 * 60
            job = claim_job(options["job"])
            if job:
                try:
                    execute_job(job)
                except Exception as error:
                    self.stderr.write(str(error))
            if options["once"]:
                return
            if not job:
                time.sleep(max(0.2, options["poll"]))
