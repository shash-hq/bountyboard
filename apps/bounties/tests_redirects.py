"""The two views that used to redirect to a raw Referer must never leave the site."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.bounties.models import Bounty

User = get_user_model()
EVIL = "https://evil.example/bounties/mod/"


class RefererRedirectTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create(username="rposter", email="rp@example.org")
        self.mod = User.objects.create(username="rmod", email="rm@example.org")
        self.mod.user_permissions.add(Permission.objects.get(codename="moderate"))
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="Redirect test bounty", description="x" * 30,
            reward=100, deadline=timezone.now() + timedelta(days=2),
        )

    # --- toggle hide -----------------------------------------------------------------

    def toggle(self, referer=None):
        self.client.force_login(self.mod)
        extra = {"HTTP_REFERER": referer} if referer else {}
        return self.client.post(reverse("bounties:toggle_hide", args=[self.bounty.pk]), **extra)

    def test_toggle_hide_ignores_an_offsite_referer(self):
        r = self.toggle(EVIL)
        self.assertRedirects(r, self.bounty.get_absolute_url(), fetch_redirect_response=False)

    def test_toggle_hide_ignores_an_offsite_referer_without_the_old_keywords(self):
        r = self.toggle("https://evil.example/anything")
        self.assertEqual(r.url, self.bounty.get_absolute_url())

    def test_toggle_hide_returns_to_the_mod_queue_on_this_site(self):
        r = self.toggle("http://testserver" + reverse("bounties:mod_queue"))
        self.assertEqual(r.url, "http://testserver" + reverse("bounties:mod_queue"))

    def test_toggle_hide_without_referer_goes_to_the_bounty(self):
        self.assertEqual(self.toggle().url, self.bounty.get_absolute_url())

    # --- layout cookie -----------------------------------------------------------------

    def test_set_layout_ignores_an_offsite_referer_but_still_sets_the_cookie(self):
        r = self.client.post(reverse("pages:set_layout"), {"layout": "list"}, HTTP_REFERER=EVIL)
        self.assertEqual(r.url, "/")
        self.assertEqual(r.cookies["bb_layout"].value, "list")

    def test_set_layout_returns_to_the_same_site_page(self):
        r = self.client.post(reverse("pages:set_layout"), {"layout": "grid"},
                             HTTP_REFERER="http://testserver/?category=Tech")
        self.assertEqual(r.url, "http://testserver/?category=Tech")

    def test_set_layout_without_referer_goes_home(self):
        self.assertEqual(self.client.post(reverse("pages:set_layout"), {"layout": "grid"}).url, "/")
