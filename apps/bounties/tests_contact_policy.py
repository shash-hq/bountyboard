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
from apps.bounties.policies import can_view_contact

User = get_user_model()

HANDLE_1 = "WA-HANDLE-ONE-91111"
HANDLE_2 = "WA-HANDLE-TWO-92222"
EMAIL_1 = "claimant.one@example.org"
EMAIL_2 = "claimant.two@example.org"
POSTER_EMAIL = "poster.private@example.org"
POSTER_HANDLE = "WA-HANDLE-POSTER-93333"


class ContactDisclosureTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user(username="cposter", email=POSTER_EMAIL, campus=True, contact_handle=POSTER_HANDLE)
        self.c1 = create_verified_user(username="cclaimant1", email=EMAIL_1, campus=True, contact_handle=HANDLE_1)
        self.c2 = create_verified_user(username="cclaimant2", email=EMAIL_2, campus=True, contact_handle=HANDLE_2)
        self.stranger = create_verified_user(username="cstranger", email="stranger@example.org")
        self.moderator = create_verified_user(username="cmod", email="mod@example.org")
        self.moderator.user_permissions.add(Permission.objects.get(codename="moderate"))
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="Contact disclosure bounty", description="x" * 30,
            reward=100, deadline=timezone.now() + timedelta(days=2),
        )
        self.claim1 = Claim.objects.create(bounty=self.bounty, claimant=self.c1, message="m" * 12)
        self.claim2 = Claim.objects.create(bounty=self.bounty, claimant=self.c2, message="m" * 12)
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
        self.assert_hidden(self.page(self.stranger), HANDLE_1, EMAIL_1, POSTER_EMAIL, POSTER_HANDLE)

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

    def test_after_accept_accepted_claimant_sees_poster_email_and_handle(self):
        self.accept_claim1()
        r = self.page(self.c1)
        self.assertContains(r, POSTER_EMAIL)
        self.assertContains(r, POSTER_HANDLE)

    def test_after_accept_rejected_claimant_sees_no_poster_contact(self):
        self.accept_claim1()
        self.assert_hidden(self.page(self.c2), POSTER_EMAIL, POSTER_HANDLE)

    def test_after_accept_moderator_still_sees_no_contact(self):
        self.accept_claim1()
        self.assert_hidden(self.page(self.moderator), HANDLE_1, EMAIL_1, HANDLE_2, EMAIL_2, POSTER_HANDLE)

    def test_after_accept_stranger_still_sees_nothing(self):
        self.accept_claim1()
        self.assert_hidden(self.page(self.stranger), HANDLE_1, EMAIL_1, POSTER_EMAIL, POSTER_HANDLE)

    def test_withdrawn_claim_contact_stays_hidden(self):
        services.withdraw_claim(self.claim2.id, self.c2)
        self.assert_hidden(self.page(self.poster), HANDLE_2, EMAIL_2)


class ContactPolicyUnitTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user(username="upposter", email="p@example.org", campus=True)
        self.c1 = create_verified_user(username="upc1", email="c1@example.org", campus=True)
        self.c2 = create_verified_user(username="upc2", email="c2@example.org", campus=True)
        self.other = create_verified_user(username="upother", email="o@example.org")
        self.moderator = create_verified_user(username="upmod", email="m@example.org")
        self.moderator.user_permissions.add(Permission.objects.get(codename="moderate"))
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="Policy unit bounty", description="x" * 30,
            reward=100, deadline=timezone.now() + timedelta(days=2),
        )
        self.claim = Claim.objects.create(bounty=self.bounty, claimant=self.c1, message="m" * 12)
        self.claim2 = Claim.objects.create(bounty=self.bounty, claimant=self.c2, message="m" * 12)

    def everyone(self):
        return {"poster": self.poster, "c1": self.c1, "c2": self.c2, "other": self.other, "mod": self.moderator}

    def allowed_pairs(self):
        people = self.everyone()
        return {
            (v, s) for v, vu in people.items() for s, su in people.items()
            if can_view_contact(vu, su, self.bounty)
        }

    def test_nobody_sees_anyones_contact_before_acceptance(self):
        self.assertEqual(self.allowed_pairs(), set())

    def test_after_acceptance_exactly_the_two_parties_see_each_other(self):
        services.accept_claim(self.claim.id, self.poster)
        self.bounty.refresh_from_db()
        # Both directions, and nothing else: not self, not the rejected claimant, not a stranger or
        # a moderator, in either direction.
        self.assertEqual(self.allowed_pairs(), {("poster", "c1"), ("c1", "poster")})

    def test_a_non_accepted_claim_status_grants_nothing(self):
        for status in (ClaimStatus.REJECTED, ClaimStatus.WITHDRAWN, ClaimStatus.PENDING):
            with self.subTest(status=status):
                Claim.objects.filter(pk=self.claim.pk).update(status=status)
                self.bounty.refresh_from_db()
                self.assertEqual(self.allowed_pairs(), set())

    def test_anonymous_never_sees_contact(self):
        from django.contrib.auth.models import AnonymousUser

        services.accept_claim(self.claim.id, self.poster)
        self.bounty.refresh_from_db()
        self.assertFalse(can_view_contact(AnonymousUser(), self.poster, self.bounty))
        self.assertFalse(can_view_contact(AnonymousUser(), self.c1, self.bounty))

    def test_contact_already_shared_survives_a_later_revocation(self):
        """Matches "contact already shown is left alone": the policy does not re-check verification."""
        from apps.accounts.models import CampusVerification

        services.accept_claim(self.claim.id, self.poster)
        CampusVerification.objects.filter(user=self.c1).update(revoked_at=timezone.now(), revoke_reason="LEFT_CAMPUS")
        self.bounty.refresh_from_db()
        self.assertTrue(can_view_contact(self.poster, self.c1, self.bounty))
