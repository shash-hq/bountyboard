"""One deterministic test per concurrency anomaly on a single bounty. No timing luck.

Each test fixes the interleaving: the main thread takes the bounty row lock, starts the "loser" in a
second thread, waits until Postgres reports it blocked on a lock, then runs the "winner" and commits.
The loser must then re-read the committed state and refuse. A service that validates before locking
has already read the stale row, so the test goes red.

Claim-vs-accept, edit-vs-accept, the open-bounty cap and revoke-vs-accept live in tests_races.py and
tests_accept_gating.py.
"""
import unittest

from django.db import connection, transaction
from django.test import TransactionTestCase

from apps.bounties import services
from apps.bounties.exceptions import InvalidTransition
from apps.bounties.models import Bounty, BountyStatus, Claim, ClaimStatus
from apps.bounties.tests_races import (
    in_worker,
    make_bounty,
    make_claim,
    make_user,
    wait_until_a_lock_request_is_waiting,
)


@unittest.skipUnless(connection.vendor == "postgresql", "race tests need PostgreSQL row locks")
class BountyLockAnomalyTests(TransactionTestCase):
    def setUp(self):
        self.poster = make_user("poster")
        self.c1 = make_user("c1_")
        self.c2 = make_user("c2_")
        self.bounty = make_bounty(self.poster)
        self.claim = make_claim(self.bounty, self.c1)

    def interleave(self, winner, loser):
        thread, outcome = in_worker(loser)
        with transaction.atomic():
            Bounty.objects.select_for_update().get(pk=self.bounty.pk)
            thread.start()
            wait_until_a_lock_request_is_waiting()
            winner()
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive(), "worker hung (deadlock?)")
        self.assertIsInstance(outcome.get("err"), InvalidTransition, outcome)
        self.bounty.refresh_from_db()
        self.claim.refresh_from_db()

    def accept(self, claim=None):
        return services.accept_claim((claim or self.claim).pk, self.poster)

    def withdraw(self):
        return services.withdraw_claim(self.claim.pk, self.c1)

    def reject(self):
        return services.reject_claim(self.claim.pk, self.poster)

    def cancel(self):
        return services.cancel_bounty(self.bounty.pk, self.poster)

    def assert_state(self, bounty_status, claim_status):
        self.assertEqual((self.bounty.status, self.claim.status), (bounty_status, claim_status))

    def test_double_accept(self):
        other = make_claim(self.bounty, self.c2)
        self.interleave(self.accept, lambda: self.accept(other))
        self.assert_state(BountyStatus.CLAIMED, ClaimStatus.ACCEPTED)
        self.assertEqual(self.bounty.accepted_claim_id, self.claim.pk)
        other.refresh_from_db()
        self.assertEqual(other.status, ClaimStatus.REJECTED)

    def test_accept_after_withdraw(self):
        self.interleave(self.withdraw, self.accept)
        self.assert_state(BountyStatus.OPEN, ClaimStatus.WITHDRAWN)

    def test_withdraw_after_accept(self):
        self.interleave(self.accept, self.withdraw)
        self.assert_state(BountyStatus.CLAIMED, ClaimStatus.ACCEPTED)

    def test_reject_after_withdraw(self):
        self.interleave(self.withdraw, self.reject)
        self.assert_state(BountyStatus.OPEN, ClaimStatus.WITHDRAWN)

    def test_accept_after_reject(self):
        self.interleave(self.reject, self.accept)
        self.assert_state(BountyStatus.OPEN, ClaimStatus.REJECTED)

    def test_accept_after_cancel(self):
        self.interleave(self.cancel, self.accept)
        self.assert_state(BountyStatus.CANCELLED, ClaimStatus.REJECTED)

    def test_cancel_after_accept(self):
        self.interleave(self.accept, self.cancel)
        self.assert_state(BountyStatus.CLAIMED, ClaimStatus.ACCEPTED)

    def test_claim_after_cancel(self):
        self.interleave(self.cancel, lambda: services.create_claim(self.bounty.pk, self.c2, "m" * 12))
        self.assertFalse(Claim.objects.filter(bounty=self.bounty, claimant=self.c2).exists())

    def test_double_submit_claim(self):
        submit = lambda: services.create_claim(self.bounty.pk, self.c2, "m" * 12)  # noqa: E731
        self.interleave(submit, submit)
        self.assertEqual(Claim.objects.filter(bounty=self.bounty, claimant=self.c2).count(), 1)

    def test_double_mark_done(self):
        self.accept()
        done = lambda: services.mark_bounty_done(self.bounty.pk, self.poster)  # noqa: E731
        self.interleave(done, done)
        self.assert_state(BountyStatus.DONE, ClaimStatus.ACCEPTED)
