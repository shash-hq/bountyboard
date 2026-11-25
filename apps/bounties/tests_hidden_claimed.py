"""A moderator hiding a claimed bounty must not strand the accepted claimant."""
from django.urls import reverse

from apps.bounties import services
from apps.bounties.models import Bounty
from apps.bounties.policies import can_view_claims, can_view_hidden_bounty
from apps.bounties.tests_revocation_cancel import Base


class HiddenClaimedBountyTests(Base):
    def setUp(self):
        super().setUp()
        Bounty.objects.filter(pk=self.bounty.pk).update(is_hidden=True)
        self.bounty.refresh_from_db()
        self.url = reverse("bounties:detail", args=[self.bounty.pk, self.bounty.slug])

    def test_the_accepted_claimant_can_still_open_it_but_not_see_the_claim_list(self):
        self.client.force_login(self.claimant)
        self.assertEqual(self.client.get(self.url).status_code, 200)
        self.assertFalse(can_view_claims(self.claimant, self.bounty))

    def test_a_stranger_cannot(self):
        self.client.force_login(self.stranger)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_access_survives_the_cancel_and_the_poster_sees_the_cancelled_badge(self):
        self.revoke(self.claimant)
        services.cancel_after_counterparty_revocation(self.bounty.pk, self.poster)
        self.bounty.refresh_from_db()
        self.assertTrue(can_view_hidden_bounty(self.claimant, self.bounty))
        self.client.force_login(self.poster)
        self.assertContains(self.client.get(self.url), '<span class="stamp-badge stamp-cancelled">CANCELLED</span>', html=True)
