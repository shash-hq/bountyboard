"""Expiry as a real state (decision 4): the sweeper closes overdue bounties and their pending claims."""
import unittest
from datetime import timedelta
from unittest import mock

from django.db import connection
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.accounts.testing import create_verified_user

from . import services
from .models import Bounty, BountyStatus, Claim, ClaimStatus, Notification, NotificationKind as K
from .tests_races import in_worker, wait_until_a_lock_request_is_waiting

PW = "pw-12345-abc"


def mk(poster, title, days=2, **kw):
    b = Bounty.objects.create(poster=poster, title=title, description="d" * 30, reward=100,
                              deadline=timezone.now() + timedelta(days=3), **kw)
    Bounty.objects.filter(pk=b.pk).update(deadline=timezone.now() + timedelta(days=days))  # fixture: bypass future-only validator
    b.refresh_from_db()
    return b


class SweepTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user("poster", "poster@gmail.com", PW, campus=True)
        self.c1 = create_verified_user("c1", "c1@gmail.com", PW, campus=True)
        self.c2 = create_verified_user("c2", "c2@gmail.com", PW, campus=True)
        self.late = mk(self.poster, "Overdue bounty one", 2)  # made overdue by each test, after its claims exist
        self.live = mk(self.poster, "Still live bounty", 2)

    def overdue(self, *bounties):
        for b in bounties:
            Bounty.objects.filter(pk=b.pk).update(deadline=timezone.now() - timedelta(hours=1))

    def test_an_overdue_open_bounty_becomes_expired_and_a_live_one_is_untouched(self):
        self.overdue(self.late)
        self.assertEqual(services.sweep_expired(), 1)
        self.late.refresh_from_db()
        self.live.refresh_from_db()
        self.assertEqual((self.late.status, self.live.status), (BountyStatus.EXPIRED, BountyStatus.OPEN))
        self.assertTrue(self.late.is_expired)

    def test_pending_claims_are_rejected_and_everyone_is_told_in_app(self):
        a = Claim.objects.create(bounty=self.late, claimant=self.c1, message="m" * 12)
        b = Claim.objects.create(bounty=self.late, claimant=self.c2, message="m" * 12)
        self.overdue(self.late)
        services.sweep_expired()
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual({a.status, b.status}, {ClaimStatus.REJECTED})
        notes = Notification.objects.filter(kind=K.BOUNTY_EXPIRED)
        self.assertEqual({n.recipient for n in notes}, {self.poster, self.c1, self.c2})
        self.assertEqual(notes.count(), 3)
        self.assertEqual(set(notes.values_list("email_status", flat=True)), {"NONE"})

    def test_other_states_and_terminal_claims_are_left_alone(self):
        done = mk(self.poster, "Finished already", -3)
        winner = Claim.objects.create(bounty=self.live, claimant=self.c2, message="m" * 12)
        Claim.objects.filter(pk=winner.pk).update(status=ClaimStatus.ACCEPTED, bounty=done)
        Bounty.objects.filter(pk=done.pk).update(status=BountyStatus.DONE, accepted_claim=winner)
        cancelled = mk(self.poster, "Cancelled already", -3)
        Bounty.objects.filter(pk=cancelled.pk).update(status=BountyStatus.CANCELLED)
        withdrawn = Claim.objects.create(bounty=self.late, claimant=self.c1, message="m" * 12)
        Claim.objects.filter(pk=withdrawn.pk).update(status=ClaimStatus.WITHDRAWN)
        self.overdue(self.late)
        services.sweep_expired()
        for b, st in ((done, "DONE"), (cancelled, "CANCELLED")):
            b.refresh_from_db()
            self.assertEqual(b.status, st)
        withdrawn.refresh_from_db()
        self.assertEqual(withdrawn.status, ClaimStatus.WITHDRAWN)
        self.assertEqual(Notification.objects.filter(recipient=self.c1).count(), 0)

    def test_it_is_idempotent(self):
        Claim.objects.create(bounty=self.late, claimant=self.c1, message="m" * 12)
        self.overdue(self.late)
        self.assertEqual(services.sweep_expired(), 1)
        self.assertEqual(services.sweep_expired(), 0)
        self.assertEqual(Notification.objects.filter(kind=K.BOUNTY_EXPIRED).count(), 2)

    def test_the_limit_bounds_one_sweep_oldest_first(self):
        second = mk(self.poster, "Overdue bounty two", 2)
        self.overdue(self.late, second)
        Bounty.objects.filter(pk=second.pk).update(deadline=timezone.now() - timedelta(days=2))  # the older deadline
        self.assertEqual(services.sweep_expired(limit=1), 1)
        second.refresh_from_db()
        self.late.refresh_from_db()
        self.assertEqual((second.status, self.late.status), (BountyStatus.EXPIRED, BountyStatus.OPEN))

    def test_one_failure_does_not_stop_the_rest(self):
        self.overdue(self.late, mk(self.poster, "Overdue bounty two", 2))
        real = services.expire_bounty
        calls = []

        def flaky(pk, now=None):
            calls.append(pk)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real(pk, now)

        with mock.patch.object(services, "expire_bounty", side_effect=flaky):
            self.assertEqual(services.sweep_expired(), 1)
        self.assertEqual(Bounty.objects.filter(status=BountyStatus.EXPIRED).count(), 1)

    def test_an_expired_bounty_cannot_be_claimed_accepted_edited_or_cancelled(self):
        self.overdue(self.late)
        services.sweep_expired()
        self.late.refresh_from_db()
        self.assertFalse(self.late.can_claim(self.c1))
        self.assertFalse(self.late.can_edit(self.poster))
        for call in (lambda: services.create_claim(self.late.pk, self.c1, "m" * 12),
                     lambda: services.cancel_bounty(self.late.pk, self.poster),
                     lambda: services.update_bounty_details(self.late.pk, self.poster, reward=500)):
            with self.assertRaises(services.InvalidTransition):
                call()

    def test_expired_bounties_do_not_count_against_the_cap(self):
        self.overdue(self.late)
        services.sweep_expired()
        self.assertEqual(services.count_live_open_bounties(self.poster), 1)

    def test_a_deadline_extended_since_is_not_expired(self):
        self.assertFalse(services.expire_bounty(self.live.pk))  # deadline in the future
        self.assertFalse(services.expire_bounty(self.late.pk))


@unittest.skipUnless(connection.vendor == "postgresql", "needs PostgreSQL row locks")
class SweepVersusAcceptRaceTests(TransactionTestCase):
    """An accept that wins the bounty lock keeps the bounty; a sweep that wins makes the accept fail."""

    def test_the_sweeper_waits_for_an_accept_and_then_leaves_the_claimed_bounty_alone(self):
        from django.db import transaction

        poster = create_verified_user("poster", "poster@gmail.com", PW, campus=True)
        claimant = create_verified_user("claimant", "claimant@gmail.com", PW, campus=True)
        b = mk(poster, "Race bounty here", 2)
        claim = Claim.objects.create(bounty=b, claimant=claimant, message="m" * 12)
        Bounty.objects.filter(pk=b.pk).update(deadline=timezone.now() - timedelta(minutes=1))  # now overdue

        worker, outcome = in_worker(lambda: services.expire_bounty(b.pk))
        with transaction.atomic():
            Bounty.objects.select_for_update().get(pk=b.pk)  # hold the bounty row, as an accept would
            worker.start()
            wait_until_a_lock_request_is_waiting()
            Claim.objects.filter(pk=claim.pk).update(status=ClaimStatus.ACCEPTED)  # the accept's effect
            Bounty.objects.filter(pk=b.pk).update(status=BountyStatus.CLAIMED, accepted_claim=claim)
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "worker hung")
        self.assertIs(outcome.get("ok"), False, outcome)
        b.refresh_from_db()
        self.assertEqual(b.status, BountyStatus.CLAIMED)
