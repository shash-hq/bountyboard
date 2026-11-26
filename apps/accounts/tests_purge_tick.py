"""The unverified-account purge runs from /internal/tick, in bounded batches (issue #14)."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts import services
from apps.accounts.testing import create_verified_user

User = get_user_model()
SECRET = "s" * 40


def abandoned(name, **kwargs):
    """An unverified account that signed up long ago and has no live link."""
    kwargs.setdefault("date_joined", timezone.now() - timedelta(days=30))
    return User.objects.create_user(name, f"{name}@gmail.com", "pw-12345-abc", **kwargs)


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
        # A listing that still includes the account although its link is now live again.
        listed = User.objects.filter(pk=user.pk)
        User.objects.filter(pk=user.pk).update(email_token_expires_at=timezone.now() + timedelta(hours=1))
        with mock.patch.object(services, "stale_unverified_users", return_value=listed):
            self.assertEqual(services.purge_stale_unverified(), 0)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_a_role_granted_after_the_listing_saves_the_account(self):
        user = abandoned("promoted")
        listed = User.objects.filter(pk=user.pk)
        user.groups.add(Group.objects.get_or_create(name="Verifiers")[0])
        with mock.patch.object(services, "stale_unverified_users", return_value=listed):
            self.assertEqual(services.purge_stale_unverified(), 0)
        self.assertTrue(User.objects.filter(pk=user.pk).exists())

    def test_the_time_budget_stops_the_scan(self):
        abandoned("gone")
        self.assertEqual(services.purge_stale_unverified(max_seconds=0), 0)
