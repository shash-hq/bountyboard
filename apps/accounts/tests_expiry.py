"""Closing lapsed verifications: recorded, audited, idempotent, and never what grants/denies access."""
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.test import TestCase, TransactionTestCase
from django.utils import timezone

from apps.bounties import services
from apps.bounties.models import Bounty, Claim, Notification
from apps.bounties.policies import is_campus_verified, open_bounty_cap
from apps.bounties.tests_races import in_worker, wait_until_a_lock_request_is_waiting

from . import expiry
from .models import AuditEvent, CampusVerification, RevokeReason
from .testing import campus_verify, create_verified_user

PW = "pw-12345-abc"
TTL = timedelta(days=settings.VERIFICATION_TTL_DAYS)


def lapsed_user(name, age_past_ttl=timedelta(days=1)):
    u = create_verified_user(name, f"{name}@gmail.com", PW)
    campus_verify(u, verified_at=timezone.now() - TTL - age_past_ttl)
    return u


class ExpireLapsedTests(TestCase):
    def test_a_lapsed_row_is_closed_as_expired_with_an_audit_event(self):
        u = lapsed_user("old")
        self.assertEqual(expiry.expire_lapsed(), 1)
        row = CampusVerification.objects.get(user=u)
        self.assertEqual((row.revoke_reason, row.revoked_by), (RevokeReason.EXPIRED, None))
        self.assertIsNotNone(row.revoked_at)
        ev = AuditEvent.objects.get(action="REVOKED", subject=u)
        self.assertEqual((ev.actor, ev.reason, ev.verification), (None, "EXPIRED", row))

    def test_a_current_row_and_an_already_revoked_row_are_left_alone(self):
        fresh = create_verified_user("fresh", "fresh@gmail.com", PW, campus=True)
        gone = lapsed_user("gone")
        CampusVerification.objects.filter(user=gone).update(revoked_at=timezone.now(), revoke_reason="LEFT_CAMPUS")
        self.assertEqual(expiry.expire_lapsed(), 0)
        self.assertIsNone(CampusVerification.objects.get(user=fresh).revoked_at)
        self.assertEqual(CampusVerification.objects.get(user=gone).revoke_reason, "LEFT_CAMPUS")

    def test_it_is_idempotent_and_limit_bounded_oldest_first(self):
        a, b = lapsed_user("a", timedelta(days=10)), lapsed_user("b", timedelta(days=1))
        self.assertEqual(expiry.expire_lapsed(limit=1), 1)
        self.assertEqual(CampusVerification.objects.get(user=a).revoke_reason, "EXPIRED")
        self.assertIsNone(CampusVerification.objects.get(user=b).revoked_at)
        self.assertEqual(expiry.expire_lapsed(), 1)
        self.assertEqual(expiry.expire_lapsed(), 0)
        self.assertEqual(AuditEvent.objects.filter(action="REVOKED").count(), 2)

    def test_one_failure_does_not_stop_the_rest(self):
        lapsed_user("x")
        lapsed_user("y")
        real = AuditEvent.objects.create
        calls = []

        def flaky(**kw):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("boom")
            return real(**kw)

        with mock.patch.object(AuditEvent.objects, "create", side_effect=flaky):
            self.assertEqual(expiry.expire_lapsed(), 1)
        self.assertEqual(CampusVerification.objects.filter(revoke_reason="EXPIRED").count(), 1)

    def test_access_is_decided_by_the_clock_not_by_the_sweeper(self):
        u = lapsed_user("clock")
        # Never swept, and already without access: a missed tick cannot extend it.
        self.assertFalse(is_campus_verified(u))
        self.assertEqual(open_bounty_cap(u), settings.OPEN_BOUNTY_CAP_EMAIL_VERIFIED)
        expiry.expire_lapsed()
        self.assertFalse(is_campus_verified(u))

    def test_expiring_notifies_nobody_and_leaves_claimed_bounties_alone(self):
        poster = lapsed_user("poster")
        claimant = create_verified_user("claimant", "claimant@gmail.com", PW, campus=True)
        b = Bounty.objects.create(poster=poster, title="Carries on after lapse", description="d" * 30, reward=100,
                                  deadline=timezone.now() + timedelta(days=3))
        c = Claim.objects.create(bounty=b, claimant=claimant, message="m" * 12)
        # The poster can't accept now (lapsed), so make the claim accepted as it would have been earlier.
        CampusVerification.objects.filter(user=poster).update(verified_at=timezone.now())
        services.accept_claim(c.pk, poster)
        CampusVerification.objects.filter(user=poster).update(verified_at=timezone.now() - TTL - timedelta(days=1))
        Notification.objects.all().delete()
        expiry.expire_lapsed()
        b.refresh_from_db()
        self.assertEqual(b.status, "CLAIMED")
        self.assertEqual(Notification.objects.count(), 0)


class ExpireVersusRenewalRaceTests(TransactionTestCase):
    """If a renewal (a fresh row, old one superseded) commits while the sweeper waits for the user
    lock, the sweeper must not touch the new row."""

    def test_the_sweeper_rechecks_under_the_user_lock(self):
        from django.db import transaction

        from .models import User

        u = lapsed_user("racer")
        old = CampusVerification.objects.get(user=u)
        worker, outcome = in_worker(lambda: expiry.expire_lapsed())
        with transaction.atomic():
            User.objects.select_for_update(no_key=True).get(pk=u.pk)
            worker.start()
            wait_until_a_lock_request_is_waiting()
            CampusVerification.objects.filter(pk=old.pk).update(verified_at=timezone.now())  # renewed in place
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "worker hung")
        self.assertEqual(outcome.get("ok"), 0, outcome)
        old.refresh_from_db()
        self.assertIsNone(old.revoked_at)
