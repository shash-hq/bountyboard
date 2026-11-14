from django.test import TestCase
from django.urls import reverse


class FoundationHealthTests(TestCase):
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
