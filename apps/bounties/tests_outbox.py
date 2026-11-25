"""The outbox: delivery, retry/backoff, budget, idempotency, and no double sends under concurrency."""
import unittest
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.db import IntegrityError, transaction
from django.db import connection
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts.testing import create_verified_user

from . import inbox, outbox, services
from .models import Bounty, Claim, EmailStatus, Notification, NotificationKind
from .tests_races import in_worker

PW = "pw-12345-abc"


def accepted_claim(suffix="a"):
    poster = create_verified_user(f"poster{suffix}", f"poster{suffix}@gmail.com", PW, campus=True)
    claimant = create_verified_user(f"claimant{suffix}", f"claimant{suffix}@gmail.com", PW, campus=True)
    poster.contact_handle = "+91 98765 43210"
    poster.save(update_fields=["contact_handle"])
    b = Bounty.objects.create(poster=poster, title=f"Outbox bounty {suffix}", description="d" * 30, reward=250,
                              deadline=timezone.now() + timedelta(days=3))
    c = Claim.objects.create(bounty=b, claimant=claimant, message="m" * 12)
    services.accept_claim(c.pk, poster)
    Notification.objects.filter(bounty=b).delete()  # accept_claim queued its own row; tests queue theirs
    mail.outbox.clear()
    c.refresh_from_db()
    return c


def queue(claim, key="k1", email=True, now=None):
    with transaction.atomic():
        n, _ = inbox.notify(claim.claimant, NotificationKind.CLAIM_ACCEPTED, "accepted", key,
                            bounty=claim.bounty, claim=claim, email=email, now=now)
    return n


class QueueTests(TestCase):
    def test_email_flag_decides_pending_vs_in_app_only(self):
        c = accepted_claim()
        self.assertEqual(queue(c, "a", email=True).email_status, EmailStatus.PENDING)
        n = queue(c, "b", email=False)
        self.assertEqual((n.email_status, n.next_attempt_at), (EmailStatus.NONE, None))

    def test_the_database_refuses_inconsistent_rows(self):
        c = accepted_claim()
        base = dict(recipient=c.claimant, kind=NotificationKind.CLAIM_ACCEPTED, message="m")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(idempotency_key="x1", email_status="PENDING", next_attempt_at=None, **base)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(idempotency_key="x2", email_status="NONE", next_attempt_at=timezone.now(), **base)
        with self.assertRaises(IntegrityError), transaction.atomic():
            Notification.objects.create(idempotency_key="x3", email_status="SENT", delivered_at=None, **base)

    def test_the_same_key_never_queues_two_emails(self):
        c = accepted_claim()
        queue(c, "same")
        queue(c, "same")
        self.assertEqual(Notification.objects.count(), 1)
        outbox.drain()
        outbox.drain()
        self.assertEqual(len(mail.outbox), 1)


class DeliveryTests(TestCase):
    def setUp(self):
        self.claim = accepted_claim()

    def test_a_due_row_is_emailed_once_with_the_posters_contact(self):
        n = queue(self.claim)
        self.assertEqual(outbox.deliver_one(), "sent")
        n.refresh_from_db()
        self.assertEqual((n.email_status, n.attempts, n.next_attempt_at), (EmailStatus.SENT, 1, None))
        self.assertIsNotNone(n.delivered_at)
        [m] = mail.outbox
        self.assertEqual(m.to, [self.claim.claimant.email])
        self.assertIn("posterA@gmail.com".lower(), m.body.lower())
        self.assertIn("+91 98765 43210", m.body)
        self.assertEqual(outbox.deliver_one(), "none")  # not re-sent
        self.assertEqual(len(mail.outbox), 1)

    def test_a_row_that_is_not_due_yet_is_left_alone(self):
        queue(self.claim, now=timezone.now() + timedelta(hours=1))
        self.assertEqual(outbox.deliver_one(), "none")
        self.assertEqual(mail.outbox, [])

    @override_settings(OUTBOX_MAX_ATTEMPTS=6)
    def test_failures_back_off_exponentially_then_give_up(self):
        n = queue(self.claim)
        now = timezone.now()
        with mock.patch("apps.bounties.outbox.EmailMessage.send", side_effect=OSError("smtp down")):
            self.assertEqual(outbox.deliver_one(now=now), "retry")
            n.refresh_from_db()
            self.assertEqual((n.attempts, n.next_attempt_at), (1, now + timedelta(minutes=1)))
            self.assertIn("smtp down", n.last_error)
            self.assertEqual(outbox.deliver_one(now=now), "none")  # still backing off
            self.assertEqual(outbox.deliver_one(now=now + timedelta(minutes=1)), "retry")
            n.refresh_from_db()
            self.assertEqual(n.next_attempt_at, now + timedelta(minutes=1) + timedelta(minutes=2))
            later = now + timedelta(days=1)
            results = []
            for i in range(10):
                later += timedelta(days=1)  # each retry is due again a day on, past any backoff
                results.append(outbox.deliver_one(now=later))
        self.assertEqual(results.count("retry"), 3)  # attempts 3, 4, 5; the 6th is the last
        self.assertEqual(results[3], "dead")
        n.refresh_from_db()
        self.assertEqual((n.email_status, n.next_attempt_at), (EmailStatus.DEAD, None))
        self.assertEqual(outbox.deliver_one(now=later + timedelta(days=9)), "none")  # dead stays dead

    def test_backoff_is_capped(self):
        self.assertEqual(outbox.backoff(1), timedelta(minutes=1))
        self.assertEqual(outbox.backoff(3), timedelta(minutes=4))
        self.assertEqual(outbox.backoff(30), timedelta(hours=6))

    def test_a_failure_then_success_delivers_exactly_once(self):
        n = queue(self.claim)
        now = timezone.now()
        with mock.patch("apps.bounties.outbox.EmailMessage.send", side_effect=OSError("blip")):
            outbox.deliver_one(now=now)
        self.assertEqual(outbox.deliver_one(now=now + timedelta(minutes=5)), "sent")
        n.refresh_from_db()
        self.assertEqual((n.attempts, n.last_error), (2, ""))
        self.assertEqual(len(mail.outbox), 1)

    @override_settings(OUTBOX_EMAIL_DAILY_CAP=1)
    def test_the_daily_budget_stops_delivery_and_leaves_rows_pending(self):
        c2 = accepted_claim("b")
        first, second = queue(self.claim, "k1"), queue(c2, "k2")
        self.assertEqual(outbox.drain(), {"sent": 1, "retry": 0, "dead": 0, "stopped": "budget"})
        second.refresh_from_db()
        self.assertEqual(second.email_status, EmailStatus.PENDING)
        # ...and the budget frees up as the 24 h window rolls on.
        self.assertEqual(outbox.deliver_one(now=timezone.now() + timedelta(hours=25)), "sent")

    def test_drain_respects_its_limit_and_reports_counts(self):
        for i in range(3):
            queue(accepted_claim(f"x{i}"), f"d{i}")
        self.assertEqual(outbox.drain(limit=2)["sent"], 2)
        self.assertEqual(Notification.objects.filter(email_status="PENDING").count(), 1)

    def test_in_app_only_rows_are_never_emailed(self):
        queue(self.claim, email=False)
        self.assertEqual(outbox.drain(), {"sent": 0, "retry": 0, "dead": 0, "stopped": "none"})
        self.assertEqual(mail.outbox, [])


class OnCommitTests(TestCase):
    def test_the_email_goes_out_after_commit_and_not_before(self):
        c = accepted_claim()
        with self.captureOnCommitCallbacks(execute=False) as callbacks:
            queue(c)
        self.assertEqual(mail.outbox, [])  # nothing sent inside the transaction
        for cb in callbacks:
            cb()
        self.assertEqual(len(mail.outbox), 1)

    def test_an_error_in_delivery_itself_never_breaks_the_request(self):
        c = accepted_claim()
        with mock.patch("apps.bounties.outbox.deliver_one", side_effect=RuntimeError("db gone")):
            with self.assertLogs("apps.bounties.outbox", "ERROR"):
                with self.captureOnCommitCallbacks(execute=True):
                    n = queue(c)  # must not raise
        n.refresh_from_db()
        self.assertEqual((n.email_status, n.attempts), (EmailStatus.PENDING, 0))

    def test_a_failing_send_leaves_the_row_for_the_tick(self):
        c = accepted_claim()
        with mock.patch("apps.bounties.outbox.EmailMessage.send", side_effect=OSError("down")):
            with self.captureOnCommitCallbacks(execute=True):
                n = queue(c)  # must not raise
        n.refresh_from_db()
        self.assertEqual((n.email_status, n.attempts), (EmailStatus.PENDING, 1))  # the tick will retry it


@unittest.skipUnless(connection.vendor == "postgresql", "needs PostgreSQL row locks")
class SkipLockedTests(TransactionTestCase):
    def test_a_row_being_delivered_elsewhere_is_skipped_not_waited_for_or_sent_twice(self):
        c = accepted_claim()
        # Created directly: going through inbox.notify would deliver on commit (these tests commit for real).
        n = Notification.objects.create(
            recipient=c.claimant, kind=NotificationKind.CLAIM_ACCEPTED, message="m", idempotency_key="race",
            bounty=c.bounty, claim=c, email_status=EmailStatus.PENDING, next_attempt_at=timezone.now(),
        )
        mail.outbox.clear()  # accept_claim still sends its own mail directly (until it moves to the outbox)
        worker, outcome = in_worker(lambda: outbox.deliver_one(n.pk))
        with transaction.atomic():
            Notification.objects.select_for_update().get(pk=n.pk)  # another ticker is mid-delivery
            worker.start()
            worker.join(timeout=15)  # it must finish by itself: skipping, not waiting on our lock
            self.assertFalse(worker.is_alive(), "worker blocked on a locked row instead of skipping it")
        self.assertEqual(outcome.get("ok"), "none", outcome)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(Notification.objects.get(pk=n.pk).email_status, EmailStatus.PENDING)
