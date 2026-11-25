"""I7: every transition writes its outbox rows in the same transaction, and only acceptance is emailed."""
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.test import TestCase
from django.utils import timezone

from apps.accounts.testing import create_verified_user

from . import inbox, services
from .exceptions import InvalidTransition
from .models import Bounty, Claim, EmailStatus, Notification, NotificationKind as K

PW = "pw-12345-abc"


class TransitionOutboxTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user("poster", "poster@gmail.com", PW, campus=True)
        self.c1 = create_verified_user("c1", "c1@gmail.com", PW, campus=True)
        self.c2 = create_verified_user("c2", "c2@gmail.com", PW, campus=True)
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="Outbox transitions", description="d" * 30, reward=100,
            deadline=timezone.now() + timedelta(days=3),
        )

    def rows(self, **kw):
        return list(Notification.objects.filter(**kw))

    def test_claiming_notifies_the_poster_in_app_only(self):
        claim = services.create_claim(self.bounty.pk, self.c1, "m" * 12)
        [n] = self.rows(kind=K.CLAIM_RECEIVED)
        self.assertEqual((n.recipient, n.claim, n.email_status), (self.poster, claim, EmailStatus.NONE))

    def test_accepting_emails_the_winner_and_tells_the_others_in_app(self):
        a = services.create_claim(self.bounty.pk, self.c1, "m" * 12)
        b = services.create_claim(self.bounty.pk, self.c2, "m" * 12)
        services.accept_claim(a.pk, self.poster)
        [won] = self.rows(kind=K.CLAIM_ACCEPTED)
        self.assertEqual((won.recipient, won.email_status), (self.c1, EmailStatus.PENDING))
        [lost] = self.rows(kind=K.CLAIM_REJECTED)
        self.assertEqual((lost.recipient, lost.claim, lost.email_status), (self.c2, b, EmailStatus.NONE))

    def test_the_claims_rejected_by_an_acceptance_record_when(self):
        a = services.create_claim(self.bounty.pk, self.c1, "m" * 12)
        b = services.create_claim(self.bounty.pk, self.c2, "m" * 12)
        long_ago = timezone.now() - timedelta(days=1)
        Claim.objects.filter(pk=b.pk).update(updated_at=long_ago)
        services.accept_claim(a.pk, self.poster)
        b.refresh_from_db()
        self.assertGreater(b.updated_at, long_ago)

    def test_an_email_goes_out_on_commit_for_acceptance_only(self):
        a = services.create_claim(self.bounty.pk, self.c1, "m" * 12)
        services.create_claim(self.bounty.pk, self.c2, "m" * 12)
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            services.accept_claim(a.pk, self.poster)
        self.assertEqual([m.to for m in mail.outbox], [["c1@gmail.com"]])

    def test_reject_withdraw_done_and_cancel_each_write_one_in_app_row(self):
        a = services.create_claim(self.bounty.pk, self.c1, "m" * 12)
        services.reject_claim(a.pk, self.poster)
        self.assertEqual([(n.recipient, n.kind) for n in self.rows(kind=K.CLAIM_REJECTED)], [(self.c1, K.CLAIM_REJECTED)])

        b2 = Bounty.objects.create(poster=self.poster, title="Second bounty", description="d" * 30, reward=100,
                                   deadline=timezone.now() + timedelta(days=3))
        w = services.create_claim(b2.pk, self.c1, "m" * 12)
        services.withdraw_claim(w.pk, self.c1)
        [n] = self.rows(kind=K.CLAIM_WITHDRAWN)
        self.assertEqual(n.recipient, self.poster)

        pending = services.create_claim(self.bounty.pk, self.c2, "m" * 12)
        services.cancel_bounty(self.bounty.pk, self.poster)
        [n] = self.rows(kind=K.BOUNTY_CANCELLED)
        self.assertEqual((n.recipient, n.claim), (self.c2, pending))

        b3 = Bounty.objects.create(poster=self.poster, title="Third bounty", description="d" * 30, reward=100,
                                   deadline=timezone.now() + timedelta(days=3))
        d = services.create_claim(b3.pk, self.c1, "m" * 12)
        services.accept_claim(d.pk, self.poster)
        services.mark_bounty_done(b3.pk, self.poster)
        [n] = self.rows(kind=K.BOUNTY_DONE)
        self.assertEqual(n.recipient, self.c1)
        self.assertTrue(all(x.email_status == EmailStatus.NONE for x in Notification.objects.exclude(kind=K.CLAIM_ACCEPTED)))

    def test_a_refused_transition_writes_nothing(self):
        a = services.create_claim(self.bounty.pk, self.c1, "m" * 12)
        before = Notification.objects.count()
        with self.assertRaises(InvalidTransition):
            services.mark_bounty_done(self.bounty.pk, self.poster)  # still OPEN
        self.assertEqual(Notification.objects.count(), before)

    def test_the_row_rolls_back_with_the_transition(self):
        a = services.create_claim(self.bounty.pk, self.c1, "m" * 12)
        before = Notification.objects.count()
        real = inbox.notify

        def write_then_fail(*args, **kwargs):
            real(*args, **kwargs)
            raise RuntimeError("boom")

        with mock.patch("apps.bounties.services.inbox.notify", side_effect=write_then_fail):
            with self.assertRaises(RuntimeError):
                services.reject_claim(a.pk, self.poster)
        a.refresh_from_db()
        self.assertEqual(a.status, "PENDING")  # the claim wasn't rejected without its notification
        self.assertEqual(Notification.objects.count(), before)

    def test_replaying_a_transition_cannot_notify_twice(self):
        a = services.create_claim(self.bounty.pk, self.c1, "m" * 12)
        services.reject_claim(a.pk, self.poster)
        with self.assertRaises(InvalidTransition):
            services.reject_claim(a.pk, self.poster)
        self.assertEqual(len(self.rows(kind=K.CLAIM_REJECTED)), 1)
