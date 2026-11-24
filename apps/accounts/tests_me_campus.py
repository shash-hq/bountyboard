"""The campus-verification panel on /me: own record and presence code (PR 5)."""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import desk
from .models import CampusVerification, RevokeReason, VerificationMethod
from .testing import create_verified_user

PW = "pw-12345-abc"
REG = "87654321"
CARD_NAME = "Asha Verma Quillfeather"


def local_date(dt):
    """How the page renders a datetime: in the active timezone (Asia/Kolkata), not UTC."""
    return timezone.localtime(dt).strftime("%b %d, %Y")


def mk(name):
    return create_verified_user(name, f"{name}@gmail.com", PW)


def in_group(user, name):
    user.groups.add(Group.objects.get(name=name))
    return user


class MeCampusPanelTests(TestCase):
    def setUp(self):
        self.user = mk("stu")
        self.client.force_login(self.user)

    def test_unverified_user_sees_the_current_presence_code(self):
        r = self.client.get(reverse("me"))
        self.assertContains(r, "not campus-verified")
        self.assertContains(r, desk.presence_code(self.user.pk))

    def test_the_code_on_me_is_accepted_by_the_desk(self):
        code = self.client.get(reverse("me")).content.decode().split('aria-label="Presence code">')[1][:6]
        vol = in_group(mk("vol"), "Verifiers")
        desk.verify_in_person(vol.pk, "stu", code, REG, CARD_NAME)
        self.assertTrue(CampusVerification.objects.filter(user=self.user).exists())

    def test_the_page_is_not_cacheable_because_it_carries_a_code(self):
        self.assertIn("no-store", self.client.get(reverse("me"))["Cache-Control"])

    def test_verified_user_sees_the_record_and_no_code(self):
        vol = in_group(mk("vol"), "Verifiers")
        desk.verify_in_person(vol.pk, "stu", desk.presence_code(self.user.pk), REG, CARD_NAME)
        r = self.client.get(reverse("me"))
        self.assertContains(r, "Verified")
        self.assertContains(r, "Valid until")
        self.assertNotContains(r, 'aria-label="Presence code"')

    def test_own_record_never_shows_card_name_or_registration_number(self):
        vol = in_group(mk("vol"), "Verifiers")
        desk.verify_in_person(vol.pk, "stu", desk.presence_code(self.user.pk), REG, CARD_NAME)
        r = self.client.get(reverse("me"))
        self.assertNotContains(r, CARD_NAME)
        self.assertNotContains(r, REG)

    def test_validity_ends_at_the_ttl(self):
        vol = in_group(mk("vol"), "Verifiers")
        v = desk.verify_in_person(vol.pk, "stu", desk.presence_code(self.user.pk), REG, CARD_NAME)
        expected = local_date(v.verified_at + timedelta(days=settings.VERIFICATION_TTL_DAYS))
        self.assertContains(self.client.get(reverse("me")), expected)

    def test_expired_verification_shows_as_expired_with_a_fresh_code(self):
        CampusVerification.objects.create(
            user=self.user, method=VerificationMethod.CAMPUS_EMAIL, verified_email=self.user.email,
            verified_at=timezone.now() - timedelta(days=366))
        r = self.client.get(reverse("me"))
        self.assertContains(r, "expired on")
        self.assertNotContains(r, "revoked")
        self.assertContains(r, desk.presence_code(self.user.pk))

    def test_revoked_verification_says_so(self):
        CampusVerification.objects.create(
            user=self.user, method=VerificationMethod.CAMPUS_EMAIL, verified_email=self.user.email,
            revoked_at=timezone.now(), revoke_reason=RevokeReason.VERIFICATION_INVALID)
        self.assertContains(self.client.get(reverse("me")), "was revoked")

    def test_expired_date_is_the_expiry_not_the_verification_date(self):
        verified = timezone.now() - timedelta(days=400)
        CampusVerification.objects.create(
            user=self.user, method=VerificationMethod.CAMPUS_EMAIL, verified_email=self.user.email, verified_at=verified)
        r = self.client.get(reverse("me"))
        expiry = local_date(verified + timedelta(days=settings.VERIFICATION_TTL_DAYS))
        self.assertContains(r, f"expired on {expiry}")

    def test_a_row_the_sweeper_closed_as_expired_still_says_expired_not_revoked(self):
        CampusVerification.objects.create(
            user=self.user, method=VerificationMethod.CAMPUS_EMAIL, verified_email=self.user.email,
            verified_at=timezone.now() - timedelta(days=400), revoked_at=timezone.now(),
            revoke_reason=RevokeReason.EXPIRED)
        r = self.client.get(reverse("me"))
        self.assertContains(r, "expired on")
        self.assertNotContains(r, "was revoked")
