"""Closing lapsed verifications: recorded, audited, idempotent, and never what grants/denies access."""
import unittest
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.db import connection
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


@unittest.skipUnless(connection.vendor == "postgresql", "needs PostgreSQL row locks")
class ExpireVersusRenewalRaceTests(TransactionTestCase):
    """If the row is renewed (here: its verified_at moved forward) while the sweeper waits for the user
    lock, the sweeper must re-check and leave it alone."""

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


class RenewalReminderTests(TestCase):
    """Reminders: once per row, inside the window, oldest first, capped per day, through the outbox."""

    def expiring(self, name, days_left, domain="lpu.co.in"):
        from .models import StudentIdentity

        u = create_verified_user(name, f"{name}@{domain}", PW)
        StudentIdentity.objects.create(user=u, reg_no_hmac=(name * 64)[:64], reg_no_last4="1234", source="SELF_DECLARED")
        campus_verify(u, verified_at=timezone.now() - TTL + timedelta(days=days_left), verified_email=u.email)
        return u

    def reminders(self):
        from apps.bounties.models import NotificationKind

        return Notification.objects.filter(kind=NotificationKind.RENEWAL_REMINDER)

    def test_only_rows_lapsing_within_the_window_are_reminded(self):
        soon = self.expiring("soon", settings.RENEWAL_REMINDER_DAYS - 1)
        self.expiring("later", settings.RENEWAL_REMINDER_DAYS + 5)
        self.expiring("edge", settings.RENEWAL_REMINDER_DAYS)
        self.assertEqual(expiry.remind_expiring(), 2)  # "edge" is exactly N days out: included
        self.assertEqual({n.recipient.username for n in self.reminders()}, {"soon", "edge"})
        self.assertEqual(self.reminders().filter(recipient=soon).get().email_status, "PENDING")

    def test_each_row_is_reminded_once(self):
        self.expiring("soon", 5)
        self.assertEqual(expiry.remind_expiring(), 1)
        self.assertEqual(expiry.remind_expiring(), 0)
        self.assertEqual(self.reminders().count(), 1)
        self.assertIsNotNone(CampusVerification.objects.get().renewal_reminder_sent_at)

    def test_lapsed_revoked_and_unreachable_users_are_skipped(self):
        lapsed = lapsed_user("lapsed")
        gone = self.expiring("gone", 5)
        CampusVerification.objects.filter(user=gone).update(revoked_at=timezone.now(), revoke_reason="LEFT_CAMPUS")
        inactive = self.expiring("inactive", 5)
        User_ = type(inactive)
        User_.objects.filter(pk=inactive.pk).update(is_active=False)
        self.assertEqual(expiry.remind_expiring(), 0)

    def test_oldest_first_and_capped_per_day_across_runs(self):
        from django.test import override_settings

        a, b, c = self.expiring("a", 3), self.expiring("b", 10), self.expiring("c", 20)
        with override_settings(RENEWAL_REMINDERS_PER_DAY=2):
            self.assertEqual(expiry.remind_expiring(), 2)
            self.assertEqual({n.recipient for n in self.reminders()}, {a, b})  # the two soonest to lapse
            self.assertEqual(expiry.remind_expiring(), 0)  # budget used up for 24 h
            self.assertEqual(expiry.remind_expiring(now=timezone.now() + timedelta(hours=25)), 1)
        self.assertEqual(self.reminders().filter(recipient=c).count(), 1)

    def test_the_email_goes_through_the_outbox_with_a_renew_link_for_campus_addresses(self):
        from django.core import mail

        from apps.bounties import outbox

        u = self.expiring("campus", 5)
        expiry.remind_expiring()
        mail.outbox.clear()
        self.assertEqual(outbox.deliver_one(), "sent")
        [m] = mail.outbox
        self.assertEqual(m.to, [u.email])
        self.assertIn("/accounts/renew/", m.body)
        self.assertNotIn(f"/accounts/renew/{u.pk}/", m.body)  # a page to sign in to, never a token link

    def test_a_gmail_user_is_told_to_visit_a_desk_instead(self):
        from django.core import mail

        from apps.bounties import outbox

        u = create_verified_user("desk", "desk@gmail.com", PW)
        # A campus-email-shaped row on a non-campus address: what a desk-verified user looks like to the
        # reminder (it only looks at the address domain to decide what advice to give).
        campus_verify(u, verified_at=timezone.now() - TTL + timedelta(days=5))
        expiry.remind_expiring()
        mail.outbox.clear()
        outbox.deliver_one()
        self.assertIn("verification desk", mail.outbox[0].body)
        self.assertNotIn("/accounts/renew/", mail.outbox[0].body)

    def test_renewing_before_the_sweep_means_no_reminder_for_the_old_row(self):
        from . import renewal

        u = self.expiring("quick", 5)
        old = CampusVerification.objects.get(user=u)
        from django.core import mail

        with self.captureOnCommitCallbacks(execute=True):
            renewal.request_renewal(u.pk)
        import re

        token = re.search(r"/accounts/renew/\d+/([\w-]+)/", mail.outbox[-1].body).group(1)
        renewal.confirm_renewal(u.pk, token)
        old.refresh_from_db()
        self.assertEqual(old.revoke_reason, "SUPERSEDED")
        self.assertEqual(expiry.remind_expiring(), 0)  # the new row is a year from lapsing

    def test_the_sweep_does_both_jobs_and_the_tick_reports_them(self):
        lapsed_user("old")
        self.expiring("soon", 5)
        self.assertEqual(expiry.sweep_verifications(), {"expired": 1, "reminded": 1})
