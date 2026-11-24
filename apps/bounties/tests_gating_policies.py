"""Tiers drive the open-bounty cap: email-verified 1, campus-verified 5, lapsed/revoked back to 1."""
from datetime import timedelta

from django.contrib.auth.models import AnonymousUser
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.accounts import campus
from apps.accounts.models import CampusVerification
from apps.accounts.testing import create_verified_user
from apps.bounties.policies import is_campus_verified, open_bounty_cap


def verified(user, **kw):
    kw.setdefault("verified_email", f"{user.username}@lpu.co.in")
    return CampusVerification.objects.create(user=user, method="CAMPUS_EMAIL", **kw)


class CapTierTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.user = create_verified_user("stu", "stu@gmail.com", "pw-12345-abc")

    def test_email_verified_user_gets_the_low_cap(self):
        self.assertFalse(is_campus_verified(self.user, self.now))
        self.assertEqual(open_bounty_cap(self.user, self.now), 1)

    def test_campus_verified_user_gets_the_full_cap(self):
        verified(self.user, verified_at=self.now)
        self.assertTrue(is_campus_verified(self.user, self.now))
        self.assertEqual(open_bounty_cap(self.user, self.now), 5)

    def test_revoked_verification_drops_back_to_the_low_cap(self):
        verified(self.user, verified_at=self.now, revoked_at=self.now, revoke_reason="LEFT_CAMPUS")
        self.assertEqual(open_bounty_cap(self.user, self.now), 1)

    def test_lapsed_verification_drops_back_without_any_sweeper(self):
        verified(self.user, verified_at=self.now - timedelta(days=366))
        self.assertEqual(open_bounty_cap(self.user, self.now), 1)
        self.assertEqual(open_bounty_cap(self.user, self.now - timedelta(days=10)), 5)

    @override_settings(OPEN_BOUNTY_CAP_EMAIL_VERIFIED=2, OPEN_BOUNTY_CAP_CAMPUS_VERIFIED=7)
    def test_caps_come_from_settings(self):
        self.assertEqual(open_bounty_cap(self.user, self.now), 2)
        verified(self.user, verified_at=self.now)
        self.assertEqual(open_bounty_cap(self.user, self.now), 7)

    def test_anonymous_is_never_verified(self):
        self.assertFalse(is_campus_verified(AnonymousUser()))
        self.assertEqual(open_bounty_cap(AnonymousUser()), 1)

    def test_policy_agrees_with_the_accounts_helper(self):
        verified(self.user, verified_at=self.now)
        self.assertEqual(is_campus_verified(self.user, self.now), campus.is_campus_verified(self.user, self.now))
