"""Absolute links in emails come from settings.SITE_URL, never a hard-coded host."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from apps.accounts.testing import create_verified_user
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.bounties.models import Bounty, Claim
from apps.bounties import emails, inbox, outbox
from apps.bounties.models import NotificationKind
from apps.bounties.notifications import absolute_url

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
        self.claim = Claim.objects.create(bounty=self.bounty, claimant=self.claimant, message="m" * 12)

    def body(self):
        from django.db import transaction

        with transaction.atomic():
            inbox.notify(self.claimant, NotificationKind.CLAIM_ACCEPTED, "accepted", "links", bounty=self.bounty,
                         claim=self.claim, email=True)
        mail.outbox.clear()
        self.assertEqual(outbox.deliver_one(), "sent")
        self.assertEqual(len(mail.outbox), 1)
        return mail.outbox[0].body

    def test_absolute_url_joins_without_a_double_slash(self):
        self.assertEqual(absolute_url("/x/"), "https://board.example.org/x/")

    def test_accepted_email_links_to_the_bounty_on_site_url_with_no_hardcoded_host(self):
        body = self.body()
        self.assertIn(f"https://board.example.org{self.bounty.get_absolute_url()}", body)
        self.assertNotIn("bountyboard.campus", body)

    def test_only_acceptance_has_an_email_renderer(self):
        """Email is scarce: every other kind is in-app only, so nothing else can be rendered or sent."""
        self.assertEqual(set(emails.RENDERERS), {"CLAIM_ACCEPTED"})
