from django.core.management.base import BaseCommand

from apps.accounts.services import purge_stale_unverified


class Command(BaseCommand):
    help = (
        "Delete unverified accounts older than the confirm-link lifetime (with no live link, no "
        "owned rows and no staff/group role), so abandoned signups cannot hold an address or "
        "username. Signup also does this lazily on a collision; this is the periodic sweep."
    )

    def handle(self, *args, **options):
        self.stdout.write(f"Purged {purge_stale_unverified()} stale unverified account(s).")
