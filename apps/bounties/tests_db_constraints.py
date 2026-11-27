"""The database itself enforces I1 and I2.

State is written with ``queryset.update()`` on purpose: it skips ``save()``/``full_clean()`` and the
services, so each rejection here comes from PostgreSQL. Remove a constraint and its test fails.
"""
import unittest
from datetime import timedelta

from django.db import IntegrityError, connection, transaction
from django.db.models import RestrictedError
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.accounts.testing import create_verified_user

from .models import Bounty, BountyStatus, Claim, ClaimStatus


class ConstraintTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.poster = create_verified_user("poster", "poster@example.com")
        cls.first = create_verified_user("first", "first@example.com")
        cls.second = create_verified_user("second", "second@example.com")

    def setUp(self):
        self.bounty = Bounty.objects.create(
            poster=self.poster,
            title="Constraint test bounty",
            description="A description long enough to pass validation.",
            reward=100,
            deadline=timezone.now() + timedelta(days=1),
        )
        self.c1 = self.claim(self.first)
        self.c2 = self.claim(self.second)

    def claim(self, user):
        return Claim.objects.create(bounty=self.bounty, claimant=user, message="I can do this task.")

    def set_claim(self, claim, status):
        Claim.objects.filter(pk=claim.pk).update(status=status)

    def set_bounty(self, **fields):
        Bounty.objects.filter(pk=self.bounty.pk).update(**fields)

    def make_accepted(self, status=BountyStatus.CLAIMED):
        self.set_claim(self.c1, ClaimStatus.ACCEPTED)
        self.set_bounty(status=status, accepted_claim=self.c1)

    def assert_rejected_by(self, constraint, write):
        with self.assertRaisesMessage(IntegrityError, constraint), transaction.atomic():
            write()


class OneAcceptedClaimTests(ConstraintTestCase):
    def test_a_second_accepted_claim_on_a_bounty_is_rejected(self):
        self.set_claim(self.c1, ClaimStatus.ACCEPTED)
        self.assert_rejected_by(
            "one_accepted_claim_per_bounty", lambda: self.set_claim(self.c2, ClaimStatus.ACCEPTED)
        )

    def test_other_statuses_may_repeat(self):
        self.set_claim(self.c1, ClaimStatus.REJECTED)
        self.set_claim(self.c2, ClaimStatus.REJECTED)


class BountyPointerTests(ConstraintTestCase):
    CONSTRAINT = "accepted_claim_iff_claimed_or_done"

    def test_claimed_or_done_without_an_accepted_claim_is_rejected(self):
        for status in (BountyStatus.CLAIMED, BountyStatus.DONE):
            with self.subTest(status=status):
                self.assert_rejected_by(self.CONSTRAINT, lambda: self.set_bounty(status=status))

    def test_open_or_cancelled_with_an_accepted_claim_is_rejected(self):
        for status in (BountyStatus.OPEN, BountyStatus.CANCELLED):
            with self.subTest(status=status):
                self.assert_rejected_by(
                    self.CONSTRAINT, lambda: self.set_bounty(status=status, accepted_claim=self.c1)
                )

    def test_claimed_and_done_with_an_accepted_claim_are_allowed(self):
        self.make_accepted(BountyStatus.CLAIMED)
        self.set_bounty(status=BountyStatus.DONE)


class DeletionTests(ConstraintTestCase):
    """``accepted_claim`` was SET_NULL, which would null the pointer on a CLAIMED row mid-delete."""

    def test_deleting_a_bounty_with_an_accepted_claim_removes_both(self):
        self.make_accepted()
        self.bounty.refresh_from_db()
        self.bounty.delete()
        self.assertFalse(Claim.objects.exists())

    def test_bulk_deleting_done_bounties_works(self):
        self.make_accepted(BountyStatus.DONE)
        Bounty.objects.filter(poster=self.poster).delete()
        self.assertFalse(Bounty.objects.exists())

    def test_deleting_only_the_accepted_claim_is_refused(self):
        self.make_accepted()
        with self.assertRaises(RestrictedError):
            self.c1.delete()

    def test_deleting_an_unaccepted_claim_is_allowed(self):
        self.make_accepted()
        self.c2.delete()
        self.assertEqual(Claim.objects.count(), 1)


@unittest.skipUnless(connection.vendor == "postgresql", "the I2 trigger exists on PostgreSQL only")
class AcceptedClaimMatchesBountyTests(ConstraintTestCase):
    """The two-table half of I2, enforced by deferred constraint triggers."""

    def assert_rejected_at_commit(self, write):
        def check():
            write()
            connection.check_constraints()  # what COMMIT would do; a TestCase never commits

        self.assert_rejected_by("accepted_claim_matches_bounty", check)

    def test_pointing_at_a_claim_that_is_not_accepted_is_rejected(self):
        self.assert_rejected_at_commit(lambda: self.set_bounty(status=BountyStatus.CLAIMED, accepted_claim=self.c1))

    def test_pointing_at_another_bountys_claim_is_rejected(self):
        other = Bounty.objects.create(
            poster=self.poster, title="Another constraint bounty", description="x" * 30, reward=100,
            deadline=timezone.now() + timedelta(days=1),
        )
        foreign = Claim.objects.create(bounty=other, claimant=self.first, message="I can do this task.")
        self.set_claim(foreign, ClaimStatus.ACCEPTED)
        self.assert_rejected_at_commit(lambda: self.set_bounty(status=BountyStatus.CLAIMED, accepted_claim=foreign))

    def test_an_accepted_claim_cannot_change_status_under_its_bounty(self):
        self.make_accepted()
        connection.check_constraints()
        for status in (ClaimStatus.PENDING, ClaimStatus.WITHDRAWN, ClaimStatus.CANCELLED):
            self.assert_rejected_at_commit(lambda status=status: self.set_claim(self.c1, status))

    def test_either_write_order_is_accepted_because_only_the_commit_is_judged(self):
        self.set_bounty(status=BountyStatus.CLAIMED, accepted_claim=self.c1)  # momentarily wrong
        self.set_claim(self.c1, ClaimStatus.ACCEPTED)
        connection.check_constraints()


@unittest.skipUnless(connection.vendor == "postgresql", "the I2 trigger exists on PostgreSQL only")
class AcceptedClaimCommitTests(TransactionTestCase):
    def test_a_real_commit_is_refused(self):
        poster = create_verified_user("poster", "poster@example.com")
        helper = create_verified_user("helper", "helper@example.com")
        bounty = Bounty.objects.create(
            poster=poster, title="Committed for real", description="x" * 30, reward=100,
            deadline=timezone.now() + timedelta(days=1),
        )
        claim = Claim.objects.create(bounty=bounty, claimant=helper, message="I can do this task.")
        with self.assertRaisesMessage(IntegrityError, "accepted_claim_matches_bounty"), transaction.atomic():
            Bounty.objects.filter(pk=bounty.pk).update(status=BountyStatus.CLAIMED, accepted_claim=claim)
        bounty.refresh_from_db()
        self.assertEqual(bounty.status, BountyStatus.OPEN)
