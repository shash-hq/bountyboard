"""/internal/tick: fails closed, authenticated in constant time, and does the scheduled work."""
from datetime import timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import Client, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.testing import create_verified_user

from . import inbox, services
from .models import Bounty, BountyStatus, Claim, EmailStatus, Notification, NotificationKind

PW = "pw-12345-abc"
SECRET = "s" * 40
URL = "/internal/tick/"


@override_settings(TICK_SECRET=SECRET)
class TickTests(TestCase):
    def setUp(self):
        cache.clear()  # rate-limit counters live in the cache and would carry over between tests
        self.poster = create_verified_user("poster", "poster@gmail.com", PW, campus=True)
        self.claimant = create_verified_user("claimant", "claimant@gmail.com", PW, campus=True)

    def overdue_bounty(self):
        b = Bounty.objects.create(poster=self.poster, title="Overdue via tick", description="d" * 30, reward=100,
                                  deadline=timezone.now() + timedelta(days=2))
        Claim.objects.create(bounty=b, claimant=self.claimant, message="m" * 12)
        Bounty.objects.filter(pk=b.pk).update(deadline=timezone.now() - timedelta(hours=1))
        return b

    def post(self, secret=SECRET, client=None):
        headers = {"HTTP_X_TICK_SECRET": secret} if secret is not None else {}
        return (client or self.client).post(URL, **headers)

    def test_reverse_matches_the_documented_path(self):
        self.assertEqual(reverse("tick"), URL)

    def test_it_fails_closed_when_no_secret_is_configured(self):
        with override_settings(TICK_SECRET=""):
            self.assertEqual(self.post(secret="").status_code, 404)
            self.assertEqual(self.post(secret=None).status_code, 404)

    def test_wrong_or_missing_secret_is_forbidden_and_does_no_work(self):
        b = self.overdue_bounty()
        for bad in (None, "", "wrong", SECRET[:-1], SECRET + "x"):
            self.assertEqual(self.post(secret=bad).status_code, 403, bad)
        b.refresh_from_db()
        self.assertEqual(b.status, BountyStatus.OPEN)

    def test_only_post_is_accepted(self):
        for method in ("get", "put", "delete"):
            self.assertEqual(getattr(self.client, method)(URL, HTTP_X_TICK_SECRET=SECRET).status_code, 405)

    def test_the_secret_is_compared_in_constant_time(self):
        with mock.patch("apps.bounties.tick.hmac.compare_digest", return_value=False) as cmp:
            self.assertEqual(self.post().status_code, 403)
        cmp.assert_called_once()

    def test_no_login_and_no_csrf_token_are_needed(self):
        client = Client(enforce_csrf_checks=True)
        self.assertEqual(self.post(client=client).status_code, 200)

    def test_it_expires_overdue_bounties_and_delivers_queued_email(self):
        b = self.overdue_bounty()
        from django.db import transaction

        with transaction.atomic():
            inbox.notify(self.claimant, NotificationKind.CLAIM_ACCEPTED, "x", "tick-mail", bounty=b,
                         claim=b.claims.first(), email=True)
        mail.outbox.clear()
        r = self.post()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["expired"], 1)
        self.assertEqual(body["outbox"]["sent"], 1)
        b.refresh_from_db()
        self.assertEqual(b.status, BountyStatus.EXPIRED)
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn(SECRET, r.content.decode())

    def test_it_also_closes_lapsed_campus_verifications(self):
        from datetime import timedelta as td

        from django.conf import settings as st

        from apps.accounts.models import CampusVerification
        from apps.accounts.testing import campus_verify

        u = create_verified_user("lapsed", "lapsed@gmail.com", PW)
        campus_verify(u, verified_at=timezone.now() - td(days=st.VERIFICATION_TTL_DAYS + 1))
        body = self.post().json()
        self.assertEqual(body["verifications"]["expired"], 1)
        self.assertEqual(CampusVerification.objects.get(user=u).revoke_reason, "EXPIRED")

    def test_repeating_it_is_harmless(self):
        self.overdue_bounty()
        self.post()
        again = self.post().json()
        self.assertEqual(again["expired"], 0)
        self.assertEqual(Notification.objects.filter(kind=NotificationKind.BOUNTY_EXPIRED).count(), 2)

    def test_a_failing_half_does_not_skip_the_other_and_reports_500(self):
        b = self.overdue_bounty()
        with mock.patch("apps.bounties.tick.outbox.drain", side_effect=RuntimeError("smtp")):
            r = self.post()
        self.assertEqual(r.status_code, 500)
        self.assertTrue(r.json()["outbox_error"])
        b.refresh_from_db()
        self.assertEqual(b.status, BountyStatus.EXPIRED)  # the sweep still ran

    def test_it_is_rate_limited_per_ip(self):
        statuses = [self.post(secret="wrong").status_code for _ in range(32)]
        self.assertEqual(statuses[:30], [403] * 30)
        self.assertEqual(statuses[30:], [429, 429])


class HealthzTests(TestCase):
    def test_healthz_never_touches_the_database(self):
        """The platform health check must not wake a paused database or fail when it is down."""
        with self.assertNumQueries(0):
            self.assertEqual(self.client.get("/healthz/").status_code, 200)
