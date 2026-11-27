"""The unverified-account purge runs from /internal/tick, in bounded batches (issue #14)."""
import threading
from datetime import timedelta
from unittest import mock, skipUnless

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import connection, transaction
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.accounts import services
from apps.accounts.testing import create_verified_user

User = get_user_model()
SECRET = "s" * 40


def abandoned(name, **kwargs):
    """An unverified account that signed up long ago and has no live link."""
    kwargs.setdefault("date_joined", timezone.now() - timedelta(days=30))
    return User.objects.create_user(name, f"{name}@gmail.com", "pw-12345-abc", **kwargs)


def stale_listing(user):
    """Stands in for the listing and yields ``user`` as already loaded, so later updates are not
    visible on it. A queryset would not do: it is lazy and would read the new values."""
    listing = mock.Mock()
    listing.order_by.return_value.iterator.return_value = [user]
    return listing


@override_settings(TICK_SECRET=SECRET)
class PurgeFromTickTests(TestCase):
    def setUp(self):
        cache.clear()

    def tick(self):
        return self.client.post("/internal/tick/", HTTP_X_TICK_SECRET=SECRET)

    def test_the_tick_purges_abandoned_accounts_only(self):
        abandoned("gone")
        abandoned("fresh", date_joined=timezone.now())
        create_verified_user("kept", "kept@gmail.com", "pw-12345-abc", date_joined=timezone.now() - timedelta(days=30))
        response = self.tick()
        self.assertEqual((response.status_code, response.json()["purged"]), (200, 1))
        self.assertEqual(set(User.objects.values_list("username", flat=True)), {"fresh", "kept"})

    def test_one_tick_deletes_a_bounded_batch_oldest_first(self):
        for i in range(3):
            abandoned(f"old{i}", date_joined=timezone.now() - timedelta(days=30 - i))
        with mock.patch("apps.bounties.tick.PURGE_PER_TICK", 2):
            self.assertEqual(self.tick().json()["purged"], 2)
            self.assertEqual(list(User.objects.values_list("username", flat=True)), ["old2"])
            self.assertEqual(self.tick().json()["purged"], 1)

    def test_a_failing_purge_is_reported_and_the_rest_still_runs(self):
        with mock.patch("apps.bounties.tick.purge_stale_unverified", side_effect=RuntimeError("boom")):
            with self.assertLogs("apps.bounties.tick", "ERROR"):
                response = self.tick()
        self.assertEqual(response.status_code, 500)
        self.assertTrue(response.json()["purged_error"])
        self.assertIn("outbox", response.json())

    def test_a_link_resent_after_the_listing_saves_the_account(self):
        user = abandoned("resent")
        # The listing was read before the resend made the link live again.
        User.objects.filter(pk=user.pk).update(email_token_expires_at=timezone.now() + timedelta(hours=1))
        with mock.patch.object(services, "stale_unverified_users", return_value=stale_listing(user)):
            self.assertEqual(services.purge_stale_unverified(), 0)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_a_role_granted_after_the_listing_saves_the_account(self):
        user = abandoned("promoted")
        user.groups.add(Group.objects.get_or_create(name="Verifiers")[0])
        with mock.patch.object(services, "stale_unverified_users", return_value=stale_listing(user)):
            self.assertEqual(services.purge_stale_unverified(), 0)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_the_time_budget_stops_the_scan(self):
        abandoned("gone")
        self.assertEqual(services.purge_stale_unverified(max_seconds=0), 0)

    def test_staff_flags_set_after_the_listing_save_the_account(self):
        for i, flag in enumerate(("is_staff", "is_superuser")):
            user = abandoned(f"flagged{i}")
            User.objects.filter(pk=user.pk).update(**{flag: True})
            with mock.patch.object(services, "stale_unverified_users", return_value=stale_listing(user)):
                self.assertEqual(services.purge_stale_unverified(), 0, flag)
            self.assertTrue(User.objects.filter(pk=user.pk).exists(), flag)

    def test_protected_accounts_use_up_the_budget_although_they_do_not_count(self):
        abandoned("protected", date_joined=timezone.now() - timedelta(days=60))
        later = abandoned("later")
        clock = [0.0]

        def slow_ownership_check(user):
            clock[0] += 10
            return True

        with mock.patch.object(services, "owns_rows", slow_ownership_check), \
                mock.patch.object(services.time, "monotonic", lambda: clock[0]):
            self.assertEqual(services.purge_stale_unverified(limit=25, max_seconds=5), 0)
        self.assertEqual(clock[0], 10)  # only the first candidate was examined
        self.assertTrue(User.objects.filter(pk=later.pk).exists())


@skipUnless(connection.vendor == "postgresql", "needs real row locks")
class PurgeSkipsLockedRowsTests(TransactionTestCase):
    def test_a_locked_account_is_skipped_instead_of_waited_for(self):
        user = abandoned("busy")
        abandoned("free")
        held, release, timed_out = threading.Event(), threading.Event(), []

        def hold_the_row():
            try:
                with transaction.atomic():
                    User.objects.select_for_update().get(pk=user.pk)
                    held.set()
                    # The timeout only stops a purge that does wait from hanging the suite.
                    if not release.wait(5):
                        timed_out.append(True)
            finally:
                connection.close()

        holder = threading.Thread(target=hold_the_row)
        holder.start()
        try:
            self.assertTrue(held.wait(5))
            self.assertEqual(services.purge_stale_unverified(), 1)
        finally:
            release.set()
            holder.join()
        self.assertEqual(timed_out, [])
        self.assertEqual(list(User.objects.values_list("username", flat=True)), ["busy"])
        self.assertEqual(services.purge_stale_unverified(), 1)  # released, so a later run takes it
