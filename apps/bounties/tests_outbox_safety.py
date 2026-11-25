"""What must not go out by email, or go wrong on the way out."""
import threading
import unittest
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts import moderation
from apps.accounts.models import User

from . import outbox, services
from .models import Bounty, BountyStatus, EmailStatus, Notification, NotificationKind
from .tests_outbox import accepted_claim, queue
from .tests_races import in_worker


class StaleAcceptanceEmailTests(TestCase):
    def test_no_contact_is_emailed_after_the_accepted_claim_is_cancelled(self):
        claim = accepted_claim()
        row = queue(claim)  # still waiting to be sent, e.g. SMTP was down
        mod = User.objects.create_superuser("mod", "mod@example.org", "pw-12345-abc")
        moderation.revoke_verification(mod.pk, claim.bounty.poster_id, "POLICY_VIOLATION")
        services.cancel_after_counterparty_revocation(claim.bounty_id, claim.claimant)

        counts = outbox.drain()

        self.assertEqual(mail.outbox, [])
        self.assertEqual(counts["skipped"], 1)
        row.refresh_from_db()
        self.assertEqual((row.email_status, row.next_attempt_at), (EmailStatus.NONE, None))
        self.assertIn("not sent", row.last_error)

    def test_it_is_still_sent_once_the_work_is_done(self):
        claim = accepted_claim()
        queue(claim)
        services.mark_bounty_done(claim.bounty_id, claim.bounty.poster)
        self.assertEqual(outbox.deliver_one(), "sent")


class EmailContentTests(TestCase):
    def test_a_newline_in_the_title_cannot_stop_delivery(self):
        claim = accepted_claim()
        Bounty.objects.filter(pk=claim.bounty_id).update(title="Fix my\r\nBcc: x@example.org")
        queue(claim)
        self.assertEqual(outbox.deliver_one(), "sent")
        self.assertNotIn("\n", mail.outbox[0].subject)

    def test_the_deadline_is_shown_in_campus_time(self):
        claim = accepted_claim()
        queue(claim)
        outbox.deliver_one()
        claim.bounty.refresh_from_db()
        local = timezone.localtime(claim.bounty.deadline)  # Asia/Kolkata, as the pages show it
        self.assertIn(f"Deadline: {local:%Y-%m-%d %H:%M}", mail.outbox[0].body)


class ExpiredFilterTests(TestCase):
    def test_the_expired_filter_lists_only_expired_bounties(self):
        claim = accepted_claim()  # a CLAIMED bounty, which must not be listed
        poster = claim.bounty.poster
        fields = dict(poster=poster, description="d" * 30, reward=100, deadline=timezone.now() + timedelta(days=3))
        swept = Bounty.objects.create(title="Swept already", **fields)
        overdue = Bounty.objects.create(title="Overdue, not swept yet", **fields)
        Bounty.objects.create(title="Still open here", **fields)
        Bounty.objects.filter(pk=swept.pk).update(status=BountyStatus.EXPIRED)
        Bounty.objects.filter(pk=overdue.pk).update(deadline=timezone.now() - timedelta(hours=1))
        self.client.force_login(poster)
        response = self.client.get(reverse("board"), {"status": "EXPIRED"})
        self.assertEqual({b.pk for b in response.context["bounties"]}, {swept.pk, overdue.pk})


@unittest.skipUnless(connection.vendor == "postgresql", "needs PostgreSQL advisory locks")
class DailyCapConcurrencyTests(TransactionTestCase):
    @override_settings(OUTBOX_EMAIL_DAILY_CAP=1)
    def test_two_deliveries_at_once_cannot_both_spend_the_last_slot(self):
        rows = []
        for suffix in ("a", "b"):
            claim = accepted_claim(suffix)
            rows.append(Notification.objects.create(
                recipient=claim.claimant, kind=NotificationKind.CLAIM_ACCEPTED, message="m",
                idempotency_key=f"cap-{suffix}", bounty=claim.bounty, claim=claim,
                email_status=EmailStatus.PENDING, next_attempt_at=timezone.now(),
            ))
        mail.outbox.clear()
        sending, release = threading.Event(), threading.Event()
        calls = []

        def slow_first_send(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                sending.set()
                release.wait(timeout=15)
            return 1

        with mock.patch("apps.bounties.outbox.EmailMessage.send", side_effect=slow_first_send):
            worker, outcome = in_worker(lambda: outbox.deliver_one(rows[0].pk))
            worker.start()
            self.assertTrue(sending.wait(timeout=15))
            second = outbox.deliver_one(rows[1].pk)  # while the first is still in SMTP
            release.set()
            worker.join(timeout=30)

        self.assertEqual((outcome.get("ok"), second), ("sent", "busy"))
        self.assertEqual(len(calls), 1)
        self.assertEqual(outbox.deliver_one(rows[1].pk), "budget")  # and afterwards the cap holds
