"""Contact disclosure (decision 3 / invariant I6): nothing before acceptance, then only the two parties."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from apps.accounts.testing import create_verified_user
from django.contrib.auth.models import Permission
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.bounties import services
from apps.bounties.models import Bounty, Claim, ClaimStatus
from apps.bounties.policies import can_view_claim_contact, can_view_poster_contact

User = get_user_model()

HANDLE_1 = "WA-HANDLE-ONE-91111"
HANDLE_2 = "WA-HANDLE-TWO-92222"
EMAIL_1 = "claimant.one@example.org"
EMAIL_2 = "claimant.two@example.org"
POSTER_EMAIL = "poster.private@example.org"


class ContactDisclosureTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user(username="cposter", email=POSTER_EMAIL)
        self.c1 = create_verified_user(username="cclaimant1", email=EMAIL_1)
        self.c2 = create_verified_user(username="cclaimant2", email=EMAIL_2)
        self.stranger = create_verified_user(username="cstranger", email="stranger@example.org")
        self.moderator = create_verified_user(username="cmod", email="mod@example.org")
        self.moderator.user_permissions.add(Permission.objects.get(codename="moderate"))
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="Contact disclosure bounty", description="x" * 30,
            reward=100, deadline=timezone.now() + timedelta(days=2),
        )
        self.claim1 = Claim.objects.create(bounty=self.bounty, claimant=self.c1, message="m" * 12, contact=HANDLE_1)
        self.claim2 = Claim.objects.create(bounty=self.bounty, claimant=self.c2, message="m" * 12, contact=HANDLE_2)
        self.url = reverse("bounties:detail", kwargs={"pk": self.bounty.pk, "slug": self.bounty.slug})

    def page(self, user):
        self.client.force_login(user)
        return self.client.get(self.url)

    def assert_hidden(self, response, *needles):
        for needle in needles:
            self.assertNotContains(response, needle)

    # --- before acceptance: nobody sees any claimant contact -----------------------------

    def test_poster_sees_no_claimant_contact_or_email_while_claims_are_pending(self):
        r = self.page(self.poster)
        self.assertContains(r, "cclaimant1")  # the claims list itself still renders
        self.assert_hidden(r, HANDLE_1, HANDLE_2, EMAIL_1, EMAIL_2)

    def test_moderator_sees_no_claimant_contact_while_claims_are_pending(self):
        r = self.page(self.moderator)
        self.assertContains(r, "cclaimant1")
        self.assert_hidden(r, HANDLE_1, HANDLE_2, EMAIL_1, EMAIL_2)

    def test_pending_claimant_sees_no_poster_email(self):
        self.assert_hidden(self.page(self.c1), POSTER_EMAIL)

    def test_stranger_sees_nothing(self):
        self.assert_hidden(self.page(self.stranger), HANDLE_1, EMAIL_1, POSTER_EMAIL)

    def test_anonymous_sees_nothing(self):
        self.client.logout()
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 302)  # the detail page needs a login
        self.assertTrue(response["Location"].startswith(reverse("accounts:login")))
        for needle in (HANDLE_1, EMAIL_1, POSTER_EMAIL):
            self.assertNotIn(needle, response.content.decode())

    # --- after acceptance: exactly the two parties ---------------------------------------

    def accept_claim1(self):
        services.accept_claim(self.claim1.id, self.poster)

    def test_after_accept_poster_sees_only_the_accepted_claimants_contact(self):
        self.accept_claim1()
        r = self.page(self.poster)
        self.assertContains(r, HANDLE_1)
        self.assertContains(r, EMAIL_1)
        self.assert_hidden(r, HANDLE_2, EMAIL_2)  # the rejected claimant stays private

    def test_after_accept_accepted_claimant_sees_poster_email(self):
        self.accept_claim1()
        self.assertContains(self.page(self.c1), POSTER_EMAIL)

    def test_after_accept_rejected_claimant_sees_no_poster_email(self):
        self.accept_claim1()
        self.assert_hidden(self.page(self.c2), POSTER_EMAIL)

    def test_after_accept_moderator_still_sees_no_contact(self):
        self.accept_claim1()
        self.assert_hidden(self.page(self.moderator), HANDLE_1, EMAIL_1, HANDLE_2, EMAIL_2)

    def test_after_accept_stranger_still_sees_nothing(self):
        self.accept_claim1()
        self.assert_hidden(self.page(self.stranger), HANDLE_1, EMAIL_1, POSTER_EMAIL)

    def test_withdrawn_claim_contact_stays_hidden(self):
        services.withdraw_claim(self.claim2.id, self.c2)
        self.assert_hidden(self.page(self.poster), HANDLE_2, EMAIL_2)


class ContactPolicyUnitTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user(username="upposter", email="p@example.org")
        self.c1 = create_verified_user(username="upc1", email="c1@example.org")
        self.other = create_verified_user(username="upother", email="o@example.org")
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="Policy unit bounty", description="x" * 30,
            reward=100, deadline=timezone.now() + timedelta(days=2),
        )
        self.claim = Claim.objects.create(bounty=self.bounty, claimant=self.c1, message="m" * 12, contact="h")

    def test_claim_contact_policy_matrix(self):
        for status, expected in [
            (ClaimStatus.PENDING, {"poster": False, "claimant": False, "other": False}),
            (ClaimStatus.REJECTED, {"poster": False, "claimant": False, "other": False}),
            (ClaimStatus.WITHDRAWN, {"poster": False, "claimant": False, "other": False}),
            (ClaimStatus.ACCEPTED, {"poster": True, "claimant": True, "other": False}),
        ]:
            self.claim.status = status
            who = {"poster": self.poster, "claimant": self.c1, "other": self.other}
            for name, user in who.items():
                with self.subTest(status=status, user=name):
                    self.assertEqual(can_view_claim_contact(user, self.claim), expected[name])

    def test_anonymous_never_sees_contact(self):
        from django.contrib.auth.models import AnonymousUser

        self.claim.status = ClaimStatus.ACCEPTED
        self.assertFalse(can_view_claim_contact(AnonymousUser(), self.claim))
        self.assertFalse(can_view_poster_contact(AnonymousUser(), self.bounty))

    def test_poster_contact_only_for_the_accepted_claimant(self):
        self.assertFalse(can_view_poster_contact(self.c1, self.bounty))
        services.accept_claim(self.claim.id, self.poster)
        self.bounty.refresh_from_db()
        self.assertTrue(can_view_poster_contact(self.c1, self.bounty))
        self.assertFalse(can_view_poster_contact(self.other, self.bounty))
        self.assertFalse(can_view_poster_contact(self.poster, self.bounty))
