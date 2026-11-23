from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse
from apps.pages.views import handler400, handler403, handler404, handler500


class FoundationAndPageTests(TestCase):
    def test_healthz_endpoint(self):
        """Verify healthz returns 200 OK."""
        response = self.client.get(reverse("pages:healthz"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content.decode("utf-8"), "OK")

    def test_robots_txt(self):
        """Verify robots.txt is accessible and contains sitemap."""
        response = self.client.get(reverse("pages:robots_txt"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("User-agent: *", response.content.decode("utf-8"))
        self.assertIn("Sitemap:", response.content.decode("utf-8"))

    def test_sitemap_xml(self):
        """Verify sitemap.xml returns 200 and valid xml markup."""
        response = self.client.get(reverse("sitemap"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/xml")
        content = response.content.decode("utf-8")
        self.assertIn("<urlset", content)
        self.assertIn("http://testserver/about/", content)

    def test_informational_pages(self):
        """Verify about, terms, privacy, guidelines return 200 OK."""
        for name in ["about", "terms", "privacy", "guidelines"]:
            response = self.client.get(reverse(f"pages:{name}"))
            self.assertEqual(response.status_code, 200)

    @override_settings(DEBUG=True)
    def test_styleguide_in_debug(self):
        """Verify styleguide is accessible in debug mode."""
        response = self.client.get(reverse("pages:styleguide"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("NEO-BRUTALIST STYLEGUIDE", response.content.decode("utf-8"))

    @override_settings(DEBUG=False)
    def test_styleguide_in_production(self):
        """Verify styleguide is 404 in production mode."""
        response = self.client.get(reverse("pages:styleguide"))
        self.assertEqual(response.status_code, 404)

    def test_layout_toggle_cookie(self):
        """Verify post to set_layout sets bb_layout cookie."""
        response = self.client.post(reverse("pages:set_layout"), {"layout": "list"}, HTTP_REFERER="/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.cookies["bb_layout"].value, "list")

    def test_custom_error_handlers(self):
        """Verify 400, 403, 404, 500 error pages render cleanly."""
        factory = RequestFactory()
        req = factory.get("/")
        
        resp400 = handler400(req)
        self.assertEqual(resp400.status_code, 400)
        self.assertIn("400", resp400.content.decode("utf-8"))

        resp403 = handler403(req)
        self.assertEqual(resp403.status_code, 403)
        self.assertIn("403", resp403.content.decode("utf-8"))

        resp404 = handler404(req)
        self.assertEqual(resp404.status_code, 404)
        self.assertIn("404", resp404.content.decode("utf-8"))

        resp500 = handler500(req)
        self.assertEqual(resp500.status_code, 500)
        self.assertIn("500", resp500.content.decode("utf-8"))


class LandingAndBoardRoutingTests(TestCase):
    def test_root_is_the_public_landing_page_for_anonymous_visitors(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertTemplateUsed(response, "pages/landing.html")
        self.assertContains(response, reverse("accounts:signup"))

    def test_signed_in_users_are_sent_from_the_root_to_the_board(self):
        from apps.accounts.testing import create_verified_user
        self.client.force_login(create_verified_user("lander", "lander@example.org", "pw-Str0ng-pass"))
        response = self.client.get("/")
        self.assertRedirects(response, reverse("board"))

    def test_board_lives_at_board_not_at_the_root(self):
        self.assertEqual(reverse("board"), "/board/")
        self.assertEqual(reverse("landing"), "/")
