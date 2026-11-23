"""Absolute links in emails come from settings.SITE_URL, never a hard-coded host."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from apps.accounts.testing import create_verified_user
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.bounties.models import Bounty, Claim
from apps.bounties.notifications import (
    absolute_url,
    send_claim_accepted_notification,
    send_claim_received_notification,
    send_claim_rejected_notification,
)

User = get_user_model()


@override_settings(SITE_URL="https://board.example.org/")  # trailing slash on purpose
class EmailLinkTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user(username="eposter", email="eposter@example.org")
        self.claimant = create_verified_user(username="eclaimant", email="eclaimant@example.org")
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="Email link bounty", description="x" * 30,
            reward=100, deadline=timezone.now() + timedelta(days=2),
        )
        self.claim = Claim.objects.create(bounty=self.bounty, claimant=self.claimant, message="m" * 12, contact="h")

    def body(self):
        self.assertEqual(len(mail.outbox), 1)
        return mail.outbox[0].body

    def test_absolute_url_joins_without_a_double_slash(self):
        self.assertEqual(absolute_url("/x/"), "https://board.example.org/x/")

    def test_received_email_links_to_the_bounty_on_site_url(self):
        send_claim_received_notification(self.claim)
        body = self.body()
        self.assertIn(f"https://board.example.org{self.bounty.get_absolute_url()}", body)
        self.assertNotIn("bountyboard.campus", body)

    def test_rejected_email_links_to_the_board_on_site_url(self):
        send_claim_rejected_notification(self.claim)
        body = self.body()
        self.assertIn("https://board.example.org/", body)
        self.assertNotIn("bountyboard.campus", body)

    def test_accepted_email_has_no_hardcoded_host(self):
        send_claim_accepted_notification(self.claim)
        self.assertNotIn("bountyboard.campus", self.body())
