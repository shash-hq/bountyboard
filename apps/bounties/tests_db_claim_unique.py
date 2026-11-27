"""I8: PostgreSQL itself refuses a second claim by the same person on one bounty."""
from datetime import timedelta

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.accounts.testing import create_verified_user

from .models import Bounty, Claim


class OneClaimPerPersonTests(TestCase):
    def test_a_second_claim_row_is_rejected_by_the_database(self):
        poster = create_verified_user("poster", "poster@example.com")
        helper = create_verified_user("helper", "helper@example.com")
        bounty = Bounty.objects.create(
            poster=poster, title="One claim each", description="x" * 30, reward=100,
            deadline=timezone.now() + timedelta(days=1),
        )
        Claim.objects.create(bounty=bounty, claimant=helper, message="I can do this task.")
        with self.assertRaisesMessage(IntegrityError, "unique_bounty_claimant"), transaction.atomic():
            # bulk_create skips save() and full_clean(), so the refusal can only come from the database.
            Claim.objects.bulk_create([Claim(bounty=bounty, claimant=helper, message="Asking a second time.")])
