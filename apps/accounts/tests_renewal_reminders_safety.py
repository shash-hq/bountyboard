"""A renewal reminder must still be true when it is sent, reach everyone due, and name the right day."""
import unittest
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest import mock

from django.conf import settings
from django.core import mail
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.bounties import inbox, outbox
from apps.bounties.models import EmailStatus, Notification, NotificationKind
from apps.bounties.tests_races import in_worker, wait_until_a_lock_request_is_waiting

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

    def test_not_after_the_account_was_deactivated(self):
        user = self.queue_reminder()
        User.objects.filter(pk=user.pk).update(is_active=False)
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


class ReminderDeliveryTests(TestCase):
    def test_the_sweep_only_queues_and_leaves_sending_to_the_bounded_drain(self):
        expiring_user("soon")
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            expiry.remind_expiring()
        self.assertEqual(mail.outbox, [])
        self.assertEqual(Notification.objects.get().email_status, EmailStatus.PENDING)

    @override_settings(RENEWAL_REMINDERS_PER_DAY=1)
    def test_unconfirmed_accounts_do_not_use_up_the_batch_either(self):
        unconfirmed = expiring_user("unconfirmed", days_left=2)
        User.objects.filter(pk=unconfirmed.pk).update(email_verified_at=None)
        active = expiring_user("active", days_left=10)
        self.assertEqual(expiry.remind_expiring(), 1)
        self.assertEqual(Notification.objects.get().recipient, active)

    @override_settings(RENEWAL_REMINDERS_PER_DAY=2)
    def test_the_cap_is_recounted_for_every_reminder(self):
        for name in ("one", "two"):
            expiring_user(name)
        other = create_verified_user("other", "other@gmail.com", PW)
        real = inbox.notify

        def another_tick_queues_one_too(*args, **kwargs):
            result = real(*args, **kwargs)
            if args[0] != other and Notification.objects.count() == 1:
                real(other, NotificationKind.RENEWAL_REMINDER, "m", "renewal-reminder:999999")
            return result

        with mock.patch("apps.bounties.inbox.notify", side_effect=another_tick_queues_one_too):
            self.assertEqual(expiry.remind_expiring(), 1)
        self.assertEqual(Notification.objects.count(), 2)


@unittest.skipUnless(connection.vendor == "postgresql", "needs PostgreSQL advisory locks")
class ReminderCapLockTests(TransactionTestCase):
    def test_a_second_tick_waits_for_the_budget(self):
        expiring_user("soon")
        worker, outcome = in_worker(expiry.remind_expiring)
        with transaction.atomic():
            with connection.cursor() as cursor:
                cursor.execute(expiry.REMINDER_BUDGET_LOCK_SQL)
            worker.start()
            wait_until_a_lock_request_is_waiting()
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "worker hung")
        self.assertEqual(outcome.get("ok"), 1, outcome)
