"""A revocation inserts notifications whose foreign keys point at the OTHER party and at their bounty.

Postgres checks those at commit with FOR KEY SHARE, which conflicts with FOR UPDATE. If any service
holds one of those rows FOR UPDATE while it waits for the row the revocation holds, the two deadlock.
User and bounty locks are therefore FOR NO KEY UPDATE: still exclusive against each other, but
compatible with foreign-key checks.
"""
import time
import unittest
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.db import connection, transaction
from django.test import TransactionTestCase
from django.utils import timezone

from apps.accounts import moderation
from apps.accounts.testing import create_verified_user
from apps.bounties import services
from apps.bounties.exceptions import NotPermitted
from apps.bounties.models import BountyStatus
from apps.bounties.tests_races import in_worker, make_bounty, make_claim, wait_until_a_lock_request_is_waiting

User = get_user_model()


@unittest.skipUnless(connection.vendor == "postgresql", "deadlock tests need PostgreSQL row locks")
class RevocationDeadlockTests(TransactionTestCase):
    def setUp(self):
        # Creation order matters: the poster has the lower id, so services lock the poster first and
        # then wait for the claimant.
        self.mod = User.objects.create_superuser("mod", "mod@example.org", "pw-12345-abc")
        self.mod2 = User.objects.create_superuser("mod2", "mod2@example.org", "pw-12345-abc")
        self.poster = create_verified_user("poster", "poster@example.org", campus=True)
        self.claimant = create_verified_user("claimant", "claimant@example.org", campus=True)
        self.bounty = make_bounty(self.poster)
        self.claim = make_claim(self.bounty, self.claimant)
        services.accept_claim(self.claim.pk, self.poster)

    def revoke(self, user, mod=None):
        return moderation.revoke_verification((mod or self.mod).pk, user.pk, "POLICY_VIOLATION")

    def wait_until_blocked_or_finished(self, thread):
        """Either it blocks on a row this transaction holds, or it has nothing to wait for and ends."""
        deadline = time.monotonic() + 10
        while thread.is_alive() and time.monotonic() < deadline:
            try:
                return wait_until_a_lock_request_is_waiting(timeout=0.2)
            except AssertionError:
                pass

    def revoke_claimant_while(self, fn):
        """``fn`` runs (as far as it can) while the claimant's row is held; then the claimant is revoked."""
        thread, outcome = in_worker(fn)
        with transaction.atomic():
            User.objects.select_for_update(no_key=True).get(pk=self.claimant.pk)
            thread.start()
            self.wait_until_blocked_or_finished(thread)
            self.revoke(self.claimant)
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive(), "worker hung")
        return outcome

    def test_cancel_against_a_revocation_of_the_canceller(self):
        self.revoke(self.poster)  # which is what lets the claimant cancel
        outcome = self.revoke_claimant_while(
            lambda: services.cancel_after_counterparty_revocation(self.bounty.pk, self.claimant))
        self.assertNotIn("err", outcome)
        self.bounty.refresh_from_db()
        self.assertEqual(self.bounty.status, BountyStatus.CANCELLED)

    def test_accept_against_a_revocation_while_the_pair_has_another_claimed_bounty(self):
        second = make_bounty(self.poster)
        claim = make_claim(second, self.claimant)
        outcome = self.revoke_claimant_while(lambda: services.accept_claim(claim.pk, self.poster))
        self.assertIsInstance(outcome.get("err"), NotPermitted, outcome)

    def test_both_parties_revoked_at_once(self):
        thread, outcome = in_worker(lambda: self.revoke(self.claimant, self.mod2))
        with transaction.atomic():
            self.revoke(self.poster)
            thread.start()
            self.wait_until_blocked_or_finished(thread)
        thread.join(timeout=30)
        self.assertFalse(thread.is_alive(), "worker hung")
        self.assertNotIn("err", outcome)

    def bounty_claimed_on_by_the_pair(self):
        """A third person's bounty with pending claims from both, the lower user id claiming first."""
        third = create_verified_user("third", "third@example.org", campus=True)
        bounty = make_bounty(third)
        make_claim(bounty, self.poster)
        make_claim(bounty, self.claimant)
        return bounty, third

    def test_cancelling_a_bounty_against_a_revocation_of_one_of_its_claimants(self):
        bounty, third = self.bounty_claimed_on_by_the_pair()
        outcome = self.revoke_claimant_while(lambda: services.cancel_bounty(bounty.pk, third))
        self.assertNotIn("err", outcome)

    def test_expiring_a_bounty_against_a_revocation_of_one_of_its_claimants(self):
        bounty, _ = self.bounty_claimed_on_by_the_pair()
        later = timezone.now() + timedelta(days=30)
        outcome = self.revoke_claimant_while(lambda: services.expire_bounty(bounty.pk, later))
        self.assertNotIn("err", outcome)
        self.assertIs(outcome.get("ok"), True, outcome)
