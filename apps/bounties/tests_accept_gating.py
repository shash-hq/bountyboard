"""I9: contact is exchanged only between two campus-verified users, checked under the user locks."""
from datetime import timedelta

from django.contrib.auth.models import Group
from django.core import mail
from django.test import TestCase, TransactionTestCase
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.accounts import moderation
from apps.accounts.models import CampusVerification, User
from apps.accounts.testing import campus_verify, create_verified_user
from apps.bounties import services
from apps.bounties.exceptions import NotPermitted
from apps.bounties.models import Bounty, BountyStatus, Claim, ClaimStatus
from apps.bounties.tests_races import in_worker, wait_until_a_lock_request_is_waiting

PW = "pw-12345-abc"


def mkbounty(poster):
    return Bounty.objects.create(
        poster=poster, title="Gating test bounty", description="d" * 30, reward=100,
        deadline=timezone.now() + timedelta(days=3),
    )


def mkclaim(bounty, claimant):
    return Claim.objects.create(bounty=bounty, claimant=claimant, message="m" * 12)


class GatingTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user("poster", "poster@gmail.com", PW, campus=True)
        self.good = create_verified_user("good", "good@gmail.com", PW, campus=True)
        self.plain = create_verified_user("plain", "plain@gmail.com", PW)  # email-verified only
        self.bounty = mkbounty(self.poster)

    def assert_untouched(self, *claims):
        self.bounty.refresh_from_db()
        self.assertEqual((self.bounty.status, self.bounty.accepted_claim_id), (BountyStatus.OPEN, None))
        for c in claims:
            c.refresh_from_db()
            self.assertEqual(c.status, ClaimStatus.PENDING)

    def test_both_verified_can_accept(self):
        c = mkclaim(self.bounty, self.good)
        services.accept_claim(c.pk, self.poster)
        self.bounty.refresh_from_db()
        self.assertEqual((self.bounty.status, self.bounty.accepted_claim_id), (BountyStatus.CLAIMED, c.pk))

    def test_an_unverified_claimant_cannot_be_accepted_and_nothing_changes(self):
        c = mkclaim(self.bounty, self.plain)
        other = mkclaim(self.bounty, self.good)
        with self.assertRaises(NotPermitted) as cm:
            services.accept_claim(c.pk, self.poster)
        self.assertIn("claimant hasn't verified", cm.exception.messages[0])
        self.assert_untouched(c, other)  # the other pending claim was not rejected as a side effect
        self.assertEqual(len(mail.outbox), 0)

    def test_an_unverified_poster_cannot_accept_anyone(self):
        poster = create_verified_user("p2", "p2@gmail.com", PW)
        b = mkbounty(poster)
        c = mkclaim(b, self.good)
        with self.assertRaises(NotPermitted) as cm:
            services.accept_claim(c.pk, poster)
        self.assertIn("Verify your campus identity", cm.exception.messages[0])
        c.refresh_from_db()
        self.assertEqual(c.status, ClaimStatus.PENDING)

    def test_a_revoked_or_lapsed_verification_does_not_count(self):
        c = mkclaim(self.bounty, self.good)
        CampusVerification.objects.filter(user=self.good).update(verified_at=timezone.now() - timedelta(days=400))
        with self.assertRaises(NotPermitted):
            services.accept_claim(c.pk, self.poster)  # lapsed, with no sweeper involved
        CampusVerification.objects.filter(user=self.good).update(
            verified_at=timezone.now(), revoked_at=timezone.now(), revoke_reason="LEFT_CAMPUS"
        )
        with self.assertRaises(NotPermitted):
            services.accept_claim(c.pk, self.poster)
        self.assert_untouched(c)

    def test_an_inactive_claimant_cannot_be_accepted(self):
        c = mkclaim(self.bounty, self.good)
        User.objects.filter(pk=self.good.pk).update(is_active=False)
        with self.assertRaises(NotPermitted):
            services.accept_claim(c.pk, self.poster)

    def test_the_poster_can_still_accept_a_verified_claimant_after_refusing_an_unverified_one(self):
        bad = mkclaim(self.bounty, self.plain)
        good = mkclaim(self.bounty, self.good)
        with self.assertRaises(NotPermitted):
            services.accept_claim(bad.pk, self.poster)
        services.accept_claim(good.pk, self.poster)
        bad.refresh_from_db()
        self.assertEqual(bad.status, ClaimStatus.REJECTED)  # rejected by the successful accept, as before

    def test_the_view_shows_the_reason_instead_of_a_500(self):
        c = mkclaim(self.bounty, self.plain)
        self.client.force_login(self.poster)
        r = self.client.post(reverse("claim_accept", args=[c.pk]), follow=True)
        self.assertContains(r, "hasn&#x27;t verified their campus identity")
        self.assert_untouched(c)


class RevokeVersusAcceptRaceTests(TransactionTestCase):
    """Never release contact to someone whose verification was revoked before the accept committed.

    Deterministic, not probabilistic: the test holds the claimant's user row (FOR NO KEY UPDATE, which
    conflicts with the service's FOR UPDATE but not with FK checks, see tests_moderation) and revokes
    their verification inside that same transaction. The accept is started and must block on the user
    lock; once released it must see the revocation. If accept_claim did not lock the user rows, the
    worker would never block (and would also read the not-yet-committed verification as valid).
    """

    def test_a_revoke_committed_while_accept_waits_makes_the_accept_fail(self):
        poster = create_verified_user("poster", "poster@gmail.com", PW, campus=True)
        claimant = create_verified_user("claimant", "claimant@gmail.com", PW, campus=True)
        # A superuser, not the Moderators group: TransactionTestCase truncates the groups that the
        # migration created, so a second test in this class would not find the group.
        mod = User.objects.create_superuser("mod", "mod@gmail.com", PW)
        bounty = mkbounty(poster)
        claim = mkclaim(bounty, claimant)

        worker, outcome = in_worker(lambda: services.accept_claim(claim.pk, poster))
        with transaction.atomic():
            User.objects.select_for_update(no_key=True).get(pk=claimant.pk)  # hold the claimant's row
            worker.start()
            wait_until_a_lock_request_is_waiting()  # the accept reached the user locks and is waiting
            moderation.revoke_verification(mod.pk, claimant.pk, "POLICY_VIOLATION")  # commits on exit
        worker.join(timeout=30)

        self.assertFalse(worker.is_alive(), "worker hung (deadlock?)")
        self.assertIsInstance(outcome.get("err"), NotPermitted, outcome)
        bounty.refresh_from_db()
        claim.refresh_from_db()
        self.assertEqual((bounty.status, bounty.accepted_claim_id, claim.status), (BountyStatus.OPEN, None, ClaimStatus.PENDING))

    def test_an_accept_that_wins_the_lock_keeps_the_exchange_and_the_revoke_waits(self):
        """The other order: the accept holds the rows first, so the revoke can only run after it. The
        contact exchange that already happened is not undone (existing CLAIMED bounties are left alone)."""
        poster = create_verified_user("poster", "poster@gmail.com", PW, campus=True)
        claimant = create_verified_user("claimant", "claimant@gmail.com", PW, campus=True)
        # A superuser, not the Moderators group: TransactionTestCase truncates the groups that the
        # migration created, so a second test in this class would not find the group.
        mod = User.objects.create_superuser("mod", "mod@gmail.com", PW)
        bounty = mkbounty(poster)
        claim = mkclaim(bounty, claimant)

        services.accept_claim(claim.pk, poster)
        moderation.revoke_verification(mod.pk, claimant.pk, "LEFT_CAMPUS")
        bounty.refresh_from_db()
        self.assertEqual((bounty.status, bounty.accepted_claim_id), (BountyStatus.CLAIMED, claim.pk))
