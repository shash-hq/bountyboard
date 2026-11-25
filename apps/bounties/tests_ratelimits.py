"""Behavioural rate-limit tests for the bounty views: request N+1 really is refused, with 429."""
from datetime import timedelta

from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.testing import create_verified_user

from .models import Bounty


class BountyViewRateLimitTests(TestCase):
    def setUp(self):
        cache.clear()  # LocMem counters are process-wide and would leak between tests
        self.addCleanup(cache.clear)
        self.poster = create_verified_user("poster", "poster@example.org")
        self.user = create_verified_user("helper", "helper@example.org")
        self.bounty = Bounty.objects.create(
            poster=self.poster,
            title="Rate limited bounty",
            description="x" * 30,
            reward=100,
            deadline=timezone.now() + timedelta(days=2),
        )
        self.client.force_login(self.user)

    def assert_limit(self, url, limit):
        # An empty POST is enough: the limiter counts requests, not successful ones.
        statuses = [self.client.post(url).status_code for _ in range(limit + 1)]
        self.assertNotIn(429, statuses[:limit], statuses)
        self.assertEqual(statuses[limit], 429, statuses)
        # The budget is per user: someone else is unaffected.
        self.client.force_login(create_verified_user("other", "other@example.org"))
        self.assertNotEqual(self.client.post(url).status_code, 429)

    def test_create_is_limited_to_10_per_hour(self):
        self.assert_limit(reverse("bounties:create"), 10)

    def test_claim_is_limited_to_20_per_hour(self):
        self.assert_limit(reverse("bounties:claim", args=[self.bounty.pk]), 20)

    def test_report_is_limited_to_10_per_hour(self):
        self.assert_limit(reverse("bounties:report", args=[self.bounty.pk]), 10)
