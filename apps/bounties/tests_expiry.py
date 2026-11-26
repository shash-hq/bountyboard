"""Expired bounties are not offered, not sorted first, and don't hold a poster's cap."""
import time
import unittest
from datetime import timedelta

from django.contrib import admin
from django.db import connection, transaction
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.testing import create_verified_user

from . import services
from .exceptions import InvalidTransition
from .models import Bounty, BountyStatus
from .tests_races import in_worker, wait_until_a_lock_request_is_waiting

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

    def test_the_admin_cannot_move_a_deadline(self):
        request = RequestFactory().get("/")
        request.user = create_verified_user("mod", "mod@gmail.com", PW)
        self.assertIn("deadline", admin.site._registry[Bounty].get_readonly_fields(request))


@unittest.skipUnless(connection.vendor == "postgresql", "race tests need PostgreSQL row locks")
class EditVersusCreateRaceTests(TransactionTestCase):
    def test_an_edit_cannot_revive_a_bounty_that_a_create_counted_as_expired(self):
        poster = create_verified_user("capper", "capper@gmail.com", PW)  # cap is 1
        old = mk(poster, "About to expire", 1)
        deadline = timezone.now() + timedelta(seconds=2)
        Bounty.objects.filter(pk=old.pk).update(deadline=deadline)
        later = timezone.now() + timedelta(days=2)
        worker, outcome = in_worker(lambda: services.update_bounty_details(old.pk, poster, deadline=later))
        with transaction.atomic():
            type(poster).objects.select_for_update().get(pk=poster.pk)  # a create in progress
            worker.start()
            wait_until_a_lock_request_is_waiting()
            time.sleep(max(0, (deadline - timezone.now()).total_seconds()) + 0.1)
            services.create_bounty(
                poster, title="Replacement", description="d" * 30, reward=100, category="Other", deadline=later,
            )
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "worker hung")
        self.assertIsInstance(outcome.get("err"), InvalidTransition, outcome)
        self.assertEqual(services.count_live_open_bounties(poster), 1)
