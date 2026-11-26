"""Expired bounties are not offered, not sorted first, and don't hold a poster's cap."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.testing import create_verified_user

from . import services
from .models import Bounty, BountyStatus

PW = "pw-12345-abc"


def mk(poster, title, days, **kw):
    b = Bounty.objects.create(
        poster=poster, title=title, description="d" * 30, reward=100,
        deadline=timezone.now() + timedelta(days=3), **kw,
    )
    Bounty.objects.filter(pk=b.pk).update(deadline=timezone.now() + timedelta(days=days))  # bypass the future-only validator
    return b


class BoardExpiryTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user("poster", "poster@gmail.com", PW)
        self.viewer = create_verified_user("viewer", "viewer@gmail.com", PW)
        self.client.force_login(self.viewer)
        self.soon = mk(self.poster, "Due soon", 1)
        self.later = mk(self.poster, "Due later", 5)
        self.dead = mk(self.poster, "Long expired", -30)
        self.cancelled = mk(self.poster, "Cancelled long ago", -10)
        Bounty.objects.filter(pk=self.cancelled.pk).update(status=BountyStatus.CANCELLED)  # fixture only

    def titles(self, query):
        return [b.title for b in self.client.get(reverse("board") + query).context["bounties"]]

    def test_deadline_soon_puts_actionable_bounties_first_soonest_first(self):
        order = self.titles("?sort=deadline_soon")
        self.assertEqual(order[:2], ["Due soon", "Due later"])
        self.assertEqual(set(order[2:]), {"Long expired", "Cancelled long ago"})

    def test_the_open_tab_does_not_offer_expired_bounties(self):
        self.assertEqual(self.titles("?status=OPEN&sort=deadline_soon"), ["Due soon", "Due later"])
        self.assertNotIn("Long expired", self.titles("?status=OPEN"))

    def test_an_expired_bounty_is_still_visible_unfiltered_and_marked(self):
        r = self.client.get(reverse("board"))
        self.assertIn("Long expired", [b.title for b in r.context["bounties"]])
        self.assertContains(r, "EXPIRED")

    def test_other_sorts_are_unchanged(self):
        self.assertEqual(self.titles("?sort=newest")[0], "Cancelled long ago")


class CapIgnoresExpiredTests(TestCase):
    def test_expired_bounties_do_not_consume_the_open_cap(self):
        poster = create_verified_user("capper", "capper@gmail.com", PW)  # email-verified cap is 1
        mk(poster, "Expired holder", -2)
        self.assertEqual(services.count_live_open_bounties(poster), 0)
        b = services.create_bounty(
            poster, title="Fresh one", description="d" * 30, reward=100, category="Other",
            deadline=timezone.now() + timedelta(days=2),
        )
        self.assertEqual(b.status, BountyStatus.OPEN)

    def test_live_and_hidden_bounties_still_count(self):
        poster = create_verified_user("capper2", "capper2@gmail.com", PW)
        mk(poster, "Hidden but live", 2, is_hidden=True)
        self.assertEqual(services.count_live_open_bounties(poster), 1)
