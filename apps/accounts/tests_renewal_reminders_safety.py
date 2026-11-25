"""A renewal reminder must still be true when it is sent, reach everyone due, and name the right day."""
from datetime import datetime, timedelta, timezone as dt_timezone

from django.conf import settings
from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.bounties import outbox
from apps.bounties.models import EmailStatus, Notification

from . import expiry, moderation
from .deletion import delete_account
from .models import User
from .testing import campus_verify, create_verified_user

PW = "pw-12345-abc"
TTL = timedelta(days=settings.VERIFICATION_TTL_DAYS)


def expiring_user(name, days_left=5):
    user = create_verified_user(name, f"{name}@gmail.com", PW)
    campus_verify(user, verified_at=timezone.now() - TTL + timedelta(days=days_left))
    return user


class StaleReminderTests(TestCase):
    def queue_reminder(self):
        user = expiring_user("soon")
        self.assertEqual(expiry.remind_expiring(), 1)  # queued; the on-commit send never runs in a TestCase
        mail.outbox.clear()
        return user

    def assert_dropped(self):
        self.assertEqual(outbox.drain().get("skipped"), 1)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(Notification.objects.get().email_status, EmailStatus.NONE)

    def test_a_reminder_that_is_still_true_is_sent(self):
        self.queue_reminder()
        self.assertEqual(outbox.deliver_one(), "sent")

    def test_not_after_the_user_has_renewed(self):
        user = self.queue_reminder()
        user.campus_verifications.update(revoked_at=timezone.now(), revoke_reason="SUPERSEDED")
        campus_verify(user)
        self.assert_dropped()

    def test_not_after_the_verification_was_revoked(self):
        user = self.queue_reminder()
        mod = User.objects.create_superuser("mod", "mod@example.org", PW)
        moderation.revoke_verification(mod.pk, user.pk, "LEFT_CAMPUS")
        self.assert_dropped()

    def test_not_after_the_account_was_deleted(self):
        user = self.queue_reminder()
        delete_account(user.pk)
        self.assert_dropped()


class ReminderBatchTests(TestCase):
    @override_settings(RENEWAL_REMINDERS_PER_DAY=1)
    def test_accounts_that_cannot_be_reminded_do_not_use_up_the_batch(self):
        inactive = expiring_user("inactive", days_left=2)  # oldest, so first in line
        User.objects.filter(pk=inactive.pk).update(is_active=False)
        active = expiring_user("active", days_left=10)
        self.assertEqual(expiry.remind_expiring(), 1)
        self.assertEqual(Notification.objects.get().recipient, active)


class ReminderDateTests(TestCase):
    def test_the_date_is_the_campus_date(self):
        # Verified at 20:00 UTC, which is already the next day in Asia/Kolkata.
        now = datetime(2027, 3, 1, 20, 0, tzinfo=dt_timezone.utc)
        user = create_verified_user("dated", "dated@gmail.com", PW)
        campus_verify(user, verified_at=now - TTL + timedelta(days=5))
        expiry.remind_expiring(now)
        self.assertIn("expires on Mar 07, 2027", Notification.objects.get().message)
