"""Default-deny access: every route needs a login unless it is on this explicit public list."""
from datetime import timedelta

from django.contrib.sessions.models import Session
from django.test import TestCase
from django.urls import URLPattern, URLResolver, get_resolver, reverse
from django.utils import timezone

from apps.accounts.testing import create_verified_user
from apps.bounties.models import Bounty

# The only routes an anonymous visitor may reach. Adding to this list is a deliberate,
# reviewed act: a new view that forgets @login_not_required fails closed (login redirect).
PUBLIC_ROUTE_NAMES = {
    "landing", "pages:about", "pages:terms", "pages:privacy", "pages:guidelines",
    "pages:healthz", "pages:healthz_bare", "pages:robots_txt", "pages:set_layout", "sitemap",
    "accounts:login", "accounts:logout", "accounts:signup",
    "accounts:verify_email", "accounts:resend_verification",
    "accounts:password_reset", "accounts:password_reset_done",
    "accounts:password_reset_confirm", "accounts:password_reset_complete",
}


def iter_named_views(patterns=None, namespace=""):
    for p in patterns if patterns is not None else get_resolver().url_patterns:
        if isinstance(p, URLResolver):
            ns = f"{namespace}{p.namespace}:" if p.namespace else namespace
            yield from iter_named_views(p.url_patterns, ns)
        elif isinstance(p, URLPattern):
            yield (f"{namespace}{p.name}" if p.name else None), p.callback


class RouteAuditTests(TestCase):
    def test_exactly_the_public_routes_are_exempt_from_login(self):
        exempt = set()
        for name, callback in iter_named_views():
            module = getattr(callback, "__module__", "") or ""
            if module.startswith(("django.contrib.admin", "debug_toolbar")):
                continue  # admin has its own login; the toolbar is dev-only
            if getattr(callback, "login_required", True) is False:
                exempt.add(name)
        self.assertEqual(exempt - {None}, PUBLIC_ROUTE_NAMES)
        self.assertNotIn(None, exempt, "an unnamed route is exempt from login")


class AnonymousAccessTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user("poster", "poster@example.org", "pw-Str0ng-pass")
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="Fix my Arduino", description="x" * 30, reward=100,
            deadline=timezone.now() + timedelta(days=2),
        )

    def assertLoginRedirect(self, response, path):
        self.assertEqual(response.status_code, 302, path)
        self.assertTrue(response["Location"].startswith(reverse("accounts:login") + "?next="), response["Location"])

    def test_public_pages_are_reachable_anonymously(self):
        for name in ["landing", "pages:about", "pages:terms", "pages:privacy", "pages:guidelines",
                     "accounts:login", "accounts:signup", "accounts:resend_verification",
                     "accounts:password_reset", "pages:healthz", "pages:robots_txt", "sitemap"]:
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("accounts:verify_email", args=["tok"])).status_code, 200)

    def test_board_detail_and_actions_require_login(self):
        gets = [reverse("board"), self.bounty.get_absolute_url(), reverse("me"), reverse("bounties:create"),
                reverse("bounties:mod_queue")]
        for path in gets:
            self.assertLoginRedirect(self.client.get(path), path)
        posts = [reverse("bounties:claim", args=[self.bounty.pk]), reverse("claim_accept", args=[1])]
        for path in posts:
            self.assertLoginRedirect(self.client.post(path), path)

    def test_anonymous_detail_views_write_no_session_rows(self):
        """Audit item: each anonymous detail hit used to create a DB session row."""
        before = Session.objects.count()
        for _ in range(3):
            self.client.get(self.bounty.get_absolute_url())
        self.assertEqual(Session.objects.count(), before)

    def test_login_with_next_returns_to_the_requested_page(self):
        response = self.client.post(
            reverse("accounts:login"),
            {"username_or_email": "poster", "password": "pw-Str0ng-pass", "next": self.bounty.get_absolute_url()},
        )
        self.assertRedirects(response, self.bounty.get_absolute_url())

    def test_admin_login_page_is_still_reachable(self):
        self.assertEqual(self.client.get("/admin/login/").status_code, 200)


class RobotsAndSitemapScopeTests(TestCase):
    def test_sitemap_lists_public_pages_only(self):
        poster = create_verified_user("poster", "poster@example.org", "pw-Str0ng-pass")
        bounty = Bounty.objects.create(
            poster=poster, title="Fix my Arduino", description="x" * 30, reward=100,
            deadline=timezone.now() + timedelta(days=2),
        )
        xml = self.client.get(reverse("sitemap")).content.decode()
        for path in ["/", "/about/", "/terms/", "/privacy/", "/guidelines/"]:
            self.assertIn(f"http://testserver{path}</loc>", xml)
        self.assertNotIn("/board/", xml)
        self.assertNotIn("/bounties/", xml)
        self.assertNotIn(bounty.slug, xml)

    def test_robots_blocks_every_gated_prefix_and_points_at_the_sitemap(self):
        body = self.client.get(reverse("pages:robots_txt")).content.decode()
        for prefix in ["/admin/", "/accounts/", "/board/", "/bounties/", "/claims/", "/me/"]:
            self.assertIn(f"Disallow: {prefix}", body)
        self.assertNotIn("Allow: /\n", body + "\n")
        self.assertIn("Sitemap:", body)
