"""The pages tell the truth about tiers: the dashboard shows the real tier and cap, and the claim list
shows who can be accepted instead of offering a button that would fail."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts.testing import create_verified_user
from apps.bounties import services
from apps.bounties.models import Bounty, Claim

PW = "pw-12345-abc"


def bounty(poster, title="Tier ui bounty"):
    return Bounty.objects.create(
        poster=poster, title=title, description="d" * 30, reward=100, deadline=timezone.now() + timedelta(days=3)
    )


class DashboardTierTests(TestCase):
    def test_email_verified_user_sees_email_tier_and_cap_one(self):
        u = create_verified_user("e", "e@gmail.com", PW)
        self.client.force_login(u)
        r = self.client.get(reverse("me"))
        self.assertContains(r, "EMAIL VERIFIED")
        self.assertNotContains(r, "CAMPUS VERIFIED")
        self.assertContains(r, "<strong>0/1</strong>")

    def test_campus_verified_user_sees_campus_tier_and_cap_five(self):
        u = create_verified_user("c", "c@gmail.com", PW, campus=True)
        self.client.force_login(u)
        r = self.client.get(reverse("me"))
        self.assertContains(r, "CAMPUS VERIFIED")
        self.assertContains(r, "<strong>0/5</strong>")

    def test_hidden_bounties_count_in_the_displayed_number(self):
        u = create_verified_user("h", "h@gmail.com", PW, campus=True)
        b = services.create_bounty(u, title="Hidden one here", description="d" * 30, reward=100,
                                   deadline=timezone.now() + timedelta(days=3))
        Bounty.objects.filter(pk=b.pk).update(is_hidden=True)
        self.client.force_login(u)
        self.assertContains(self.client.get(reverse("me")), "<strong>1/5</strong>")


class ClaimListTests(TestCase):
    def setUp(self):
        self.verified_poster = create_verified_user("vp", "vp@gmail.com", PW, campus=True)
        self.plain_poster = create_verified_user("pp", "pp@gmail.com", PW)
        self.good = create_verified_user("good", "good@gmail.com", PW, campus=True)
        self.plain = create_verified_user("plain", "plain@gmail.com", PW)

    def page(self, poster, *claimants):
        b = bounty(poster)
        claims = [Claim.objects.create(bounty=b, claimant=c, message="m" * 12) for c in claimants]
        self.client.force_login(poster)
        return self.client.get(b.get_absolute_url()), claims

    def test_verified_poster_gets_accept_only_for_the_verified_claimant(self):
        r, (good, plain) = self.page(self.verified_poster, self.good, self.plain)
        self.assertContains(r, "CAMPUS VERIFIED")
        self.assertContains(r, "NOT CAMPUS-VERIFIED")
        self.assertContains(r, f"/claims/{good.pk}/accept/")
        self.assertNotContains(r, f"/claims/{plain.pk}/accept/")
        self.assertContains(r, "hasn't verified their campus identity yet")
        self.assertContains(r, f"/claims/{plain.pk}/reject/")  # they can still be rejected

    def test_unverified_poster_gets_no_accept_buttons_and_is_told_why(self):
        r, (good,) = self.page(self.plain_poster, self.good)
        self.assertNotContains(r, f"/claims/{good.pk}/accept/")
        self.assertContains(r, "once your campus identity is verified")
        self.assertContains(r, f"/claims/{good.pk}/reject/")

    def test_the_server_still_refuses_even_if_the_button_is_forged(self):
        b = bounty(self.plain_poster)
        c = Claim.objects.create(bounty=b, claimant=self.good, message="m" * 12)
        self.client.force_login(self.plain_poster)
        r = self.client.post(reverse("claim_accept", args=[c.pk]), follow=True)
        self.assertContains(r, "Verify your campus identity")
        c.refresh_from_db()
        self.assertEqual(c.status, "PENDING")
