from django.contrib import admin
from django.test import RequestFactory, TestCase

from apps.accounts.testing import create_verified_user

from .models import Bounty


class AdminActionTests(TestCase):
    def test_admin_offers_no_bulk_mark_done_action(self):
        request = RequestFactory().get("/")
        request.user = create_verified_user("root", "root@example.com", is_staff=True, is_superuser=True)
        actions = admin.site._registry[Bounty].get_actions(request)
        self.assertIn("hide_bounties", actions)
        self.assertNotIn("mark_bounties_done", actions)
