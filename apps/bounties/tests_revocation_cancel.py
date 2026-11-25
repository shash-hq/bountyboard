"""Revocation consequences: the counterparty is notified, and may cancel (and only then)."""
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import Group
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts import moderation
from apps.accounts.errors import InvalidState
from apps.accounts.models import CampusVerification, User
from apps.accounts.testing import campus_verify, create_verified_user
from apps.bounties import services
from apps.bounties.exceptions import InvalidTransition, NotPermitted
from apps.bounties.models import Bounty, BountyStatus, Claim, ClaimStatus, Notification, NotificationKind
from apps.bounties.tests_races import in_worker, wait_until_a_lock_request_is_waiting

PW = "pw-12345-abc"


def mkbounty(poster, title="Revocation bounty"):
    return Bounty.objects.create(
        poster=poster, title=title, description="d" * 30, reward=100,
        deadline=timezone.now() + timedelta(days=3),
    )


def mkclaim(bounty, claimant):
    return Claim.objects.create(bounty=bounty, claimant=claimant, message="m" * 12)


class Base(TestCase):
    def setUp(self):
        self.mod = create_verified_user("mod", "mod@gmail.com", PW)
        self.mod.groups.add(Group.objects.get(name="Moderators"))
        self.poster = create_verified_user("poster", "poster@gmail.com", PW, campus=True)
        self.claimant = create_verified_user("claimant", "claimant@gmail.com", PW, campus=True)
        self.stranger = create_verified_user("stranger", "stranger@gmail.com", PW, campus=True)
        self.bounty = mkbounty(self.poster)
        self.claim = mkclaim(self.bounty, self.claimant)
        services.accept_claim(self.claim.pk, self.poster)
        Notification.objects.all().delete()  # drop the transition's own rows; these tests count revocation ones

    def revoke(self, user, reason="POLICY_VIOLATION"):
        return moderation.revoke_verification(self.mod.pk, user.pk, reason)

    def assert_still_claimed(self):
        self.bounty.refresh_from_db()
        self.claim.refresh_from_db()
        self.assertEqual(self.bounty.status, BountyStatus.CLAIMED)
        self.assertEqual(self.bounty.accepted_claim_id, self.claim.pk)
        self.assertEqual(self.claim.status, ClaimStatus.ACCEPTED)


class RevokeNotifiesCounterpartyTests(Base):
    def test_policy_and_invalid_notify_the_poster_when_the_claimant_is_revoked(self):
        for i, reason in enumerate(["POLICY_VIOLATION", "VERIFICATION_INVALID"]):
            user = create_verified_user(f"c{i}", f"c{i}@gmail.com", PW, campus=True)
            b = mkbounty(self.poster, f"Claimed {i}")
            c = mkclaim(b, user)
            services.accept_claim(c.pk, self.poster)
            self.revoke(user, reason)
            note = Notification.objects.get(recipient=self.poster, bounty=b)
            self.assertEqual(note.kind, NotificationKind.COUNTERPARTY_REVOKED)

    def test_it_notifies_the_claimant_when_the_poster_is_revoked(self):
        self.revoke(self.poster)
        self.assertEqual(Notification.objects.filter(recipient=self.claimant, bounty=self.bounty).count(), 1)
        self.assertEqual(Notification.objects.filter(recipient=self.poster).count(), 0)

    def test_left_campus_notifies_nobody(self):
        self.revoke(self.claimant, "LEFT_CAMPUS")
        self.assertEqual(Notification.objects.count(), 0)

    def test_only_claimed_bounties_are_reported(self):
        mkbounty(self.claimant, "Open one")  # OPEN, claimant is its poster
        self.revoke(self.claimant)
        self.assertEqual(list(Notification.objects.values_list("bounty__title", flat=True)), [self.bounty.title])

    def test_the_notification_commits_or_rolls_back_with_the_revocation(self):
        with mock.patch.object(moderation, "_notify_counterparties", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                self.revoke(self.claimant)
        self.assertEqual(Notification.objects.count(), 0)
        self.assertTrue(CampusVerification.objects.get(user=self.claimant).revoked_at is None)

    def test_a_failed_revoke_notifies_nobody(self):
        self.revoke(self.claimant)
        n = Notification.objects.count()
        with self.assertRaises(InvalidState):  # no active verification any more
            self.revoke(self.claimant)
        self.assertEqual(Notification.objects.count(), n)


class CancelAfterRevocationTests(Base):
    def test_poster_can_cancel_after_the_claimant_is_revoked(self):
        self.revoke(self.claimant)
        services.cancel_after_counterparty_revocation(self.bounty.pk, self.poster)
        self.bounty.refresh_from_db()
        self.claim.refresh_from_db()
        self.assertEqual((self.bounty.status, self.bounty.accepted_claim_id), (BountyStatus.CANCELLED, None))
        self.assertEqual(self.claim.status, ClaimStatus.CANCELLED)
        self.assertEqual(
            Notification.objects.filter(recipient=self.claimant, kind=NotificationKind.COUNTERPARTY_CANCELLED).count(), 1
        )

    def test_claimant_can_cancel_after_the_poster_is_revoked(self):
        self.revoke(self.poster, "VERIFICATION_INVALID")
        services.cancel_after_counterparty_revocation(self.bounty.pk, self.claimant)
        self.bounty.refresh_from_db()
        self.assertEqual((self.bounty.status, self.bounty.accepted_claim_id), (BountyStatus.CANCELLED, None))

    def test_no_penalty_is_recorded_and_the_other_partys_verification_is_untouched(self):
        self.revoke(self.claimant)
        before = CampusVerification.objects.filter(user=self.poster, revoked_at__isnull=True).count()
        services.cancel_after_counterparty_revocation(self.bounty.pk, self.poster)
        self.assertEqual(CampusVerification.objects.filter(user=self.poster, revoked_at__isnull=True).count(), before)

    def test_refused_while_the_other_party_is_still_verified(self):
        with self.assertRaises(NotPermitted):
            services.cancel_after_counterparty_revocation(self.bounty.pk, self.poster)
        self.assert_still_claimed()

    def test_refused_for_left_campus(self):
        self.revoke(self.claimant, "LEFT_CAMPUS")
        with self.assertRaises(NotPermitted):
            services.cancel_after_counterparty_revocation(self.bounty.pk, self.poster)
        self.assert_still_claimed()

    def test_refused_when_the_verification_merely_expired(self):
        CampusVerification.objects.filter(user=self.claimant).update(verified_at=timezone.now() - timedelta(days=4000))
        with self.assertRaises(NotPermitted):
            services.cancel_after_counterparty_revocation(self.bounty.pk, self.poster)
        self.assert_still_claimed()

    def test_refused_once_the_other_party_has_re_verified(self):
        self.revoke(self.claimant)
        campus_verify(self.claimant)
        with self.assertRaises(NotPermitted):
            services.cancel_after_counterparty_revocation(self.bounty.pk, self.poster)
        self.assert_still_claimed()

    def test_the_revoked_party_cannot_cancel_for_themselves(self):
        self.revoke(self.claimant)
        with self.assertRaises(NotPermitted):
            services.cancel_after_counterparty_revocation(self.bounty.pk, self.claimant)
        self.assert_still_claimed()

    def test_a_third_party_cannot_cancel(self):
        self.revoke(self.claimant)
        with self.assertRaises(NotPermitted):
            services.cancel_after_counterparty_revocation(self.bounty.pk, self.stranger)
        self.assert_still_claimed()

    def test_only_a_claimed_bounty_can_be_cancelled_this_way_and_only_once(self):
        open_bounty = mkbounty(self.poster, "Still open")
        with self.assertRaises(InvalidTransition):
            services.cancel_after_counterparty_revocation(open_bounty.pk, self.poster)
        self.revoke(self.claimant)
        services.cancel_after_counterparty_revocation(self.bounty.pk, self.poster)
        with self.assertRaises(InvalidTransition):
            services.cancel_after_counterparty_revocation(self.bounty.pk, self.poster)
        self.assertEqual(Notification.objects.filter(kind=NotificationKind.COUNTERPARTY_CANCELLED).count(), 1)

    def test_a_failure_after_the_state_change_rolls_everything_back(self):
        self.revoke(self.claimant)
        with mock.patch("apps.bounties.services.inbox.notify", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                services.cancel_after_counterparty_revocation(self.bounty.pk, self.poster)
        self.assert_still_claimed()


class CancelViewTests(Base):
    def setUp(self):
        super().setUp()
        self.url = reverse("bounties:cancel_revoked", args=[self.bounty.pk])

    def test_the_button_shows_only_to_a_party_and_only_after_a_qualifying_revocation(self):
        self.client.force_login(self.poster)
        self.assertNotContains(self.client.get(self.bounty.get_absolute_url()), self.url)
        self.revoke(self.claimant)
        self.assertContains(self.client.get(self.bounty.get_absolute_url()), self.url)
        self.client.force_login(self.stranger)
        self.assertNotContains(self.client.get(self.bounty.get_absolute_url()), self.url)

    def test_post_cancels_and_redirects_back(self):
        self.revoke(self.claimant)
        self.client.force_login(self.poster)
        self.assertRedirects(self.client.post(self.url), self.bounty.get_absolute_url())
        self.bounty.refresh_from_db()
        self.assertEqual(self.bounty.status, BountyStatus.CANCELLED)

    def test_get_is_not_allowed_and_anonymous_is_sent_to_login(self):
        self.client.force_login(self.poster)
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.client.logout()
        self.assertEqual(self.client.post(self.url).status_code, 302)
        self.assert_still_claimed()

    def test_a_refused_cancel_shows_a_message_and_changes_nothing(self):
        self.client.force_login(self.poster)  # claimant still verified
        r = self.client.post(self.url, follow=True)
        self.assertContains(r, "revoked")
        self.assert_still_claimed()


class CancelVersusReVerifyRaceTests(TransactionTestCase):
    """The cancel is decided on the locked user rows, so a re-verification that commits first wins.

    The test holds the claimant's user row and re-verifies them inside that transaction; the cancel
    must block on the user lock, then see the new active verification and refuse. If the policy were
    evaluated before the user locks were taken, it would read the revoked state and cancel.
    """

    def test_a_reverification_committed_while_the_cancel_waits_makes_it_fail(self):
        poster = create_verified_user("poster", "poster@gmail.com", PW, campus=True)
        claimant = create_verified_user("claimant", "claimant@gmail.com", PW, campus=True)
        mod = User.objects.create_superuser("mod", "mod@gmail.com", PW)
        bounty = mkbounty(poster)
        claim = mkclaim(bounty, claimant)
        services.accept_claim(claim.pk, poster)
        moderation.revoke_verification(mod.pk, claimant.pk, "POLICY_VIOLATION")

        worker, outcome = in_worker(lambda: services.cancel_after_counterparty_revocation(bounty.pk, poster))
        with transaction.atomic():
            User.objects.select_for_update(no_key=True).get(pk=claimant.pk)
            worker.start()
            wait_until_a_lock_request_is_waiting()
            campus_verify(claimant)  # commits on exit
        worker.join(timeout=30)

        self.assertFalse(worker.is_alive(), "worker hung (deadlock?)")
        self.assertIsInstance(outcome.get("err"), NotPermitted, outcome)
        bounty.refresh_from_db()
        self.assertEqual((bounty.status, bounty.accepted_claim_id), (BountyStatus.CLAIMED, claim.pk))


class StaffBadgeTests(Base):
    """The badge needs the staff role AND an active campus verification, checked at render time."""

    def badge(self):
        self.client.force_login(self.stranger)
        return b"STAFF</span>" in self.client.get(self.bounty.get_absolute_url()).content

    def test_role_and_verification_both_required(self):
        self.assertFalse(self.badge())  # verified, not staff
        moderation.grant_staff(self.mod.pk, self.poster.pk, "faculty")
        self.assertTrue(self.badge())
        self.revoke(self.poster, "LEFT_CAMPUS")
        self.assertFalse(self.badge())  # staff role kept, verification gone

    def test_staff_without_campus_verification_gets_no_badge(self):
        bare = create_verified_user("bare", "bare@gmail.com", PW)
        moderation.grant_staff(self.mod.pk, bare.pk)
        b = mkbounty(bare, "Bare staff bounty")
        self.client.force_login(self.stranger)
        self.assertNotIn(b"STAFF</span>", self.client.get(b.get_absolute_url()).content)

    def test_removing_the_role_removes_the_badge(self):
        moderation.grant_staff(self.mod.pk, self.poster.pk)
        moderation.revoke_staff(self.mod.pk, self.poster.pk)
        self.assertFalse(self.badge())
