"""The CAMPUS_EMAIL path: exact-domain match, reg-no binding, collisions, TTL, renewal."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from . import campus
from .identity import InvalidRegNo, hash_reg_no
from .models import AuditEvent, CampusVerification, IdentityReview, StudentIdentity
from .services import InvalidToken, confirm_email
from .tests_email_verification import PW, issue_token
from .tests_ratelimits import limits

User = get_user_model()
REG = "12345678"


class CampusEmailDomainTests(TestCase):
    def test_only_an_exact_domain_match_counts(self):
        yes = ["a@lpu.co.in", "A@LPU.CO.IN", " a@lpu.co.in "]
        no = [
            "a@notlpu.co.in",  # the old endswith() bug
            "a@lpu.co.in.evil.com",
            "a@mail.lpu.co.in",
            "a@lpu.co.in@evil.com",
            "a@evil.com@lpu.co.in",
            "@lpu.co.in",
            "lpu.co.in",
            "",
            None,
        ]
        for e in yes:
            with self.subTest(e=e):
                self.assertTrue(campus.campus_email_allowed(e))
        for e in no:
            with self.subTest(e=e):
                self.assertFalse(campus.campus_email_allowed(e))

    @override_settings(ALLOWED_CAMPUS_DOMAINS=["lpu.co.in", "lpu.in"])
    def test_domains_come_from_settings(self):
        self.assertTrue(campus.campus_email_allowed("a@lpu.in"))


class ActiveVerificationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("stu", "stu@lpu.co.in", PW)
        self.now = timezone.now()

    def verify(self, verified_at, **kw):
        return CampusVerification.objects.create(
            user=self.user, method="CAMPUS_EMAIL", verified_email="stu@lpu.co.in",
            verified_at=verified_at, **kw,
        )

    @override_settings(VERIFICATION_TTL_DAYS=365)
    def test_expiry_is_time_based_without_any_sweeper(self):
        ttl = timedelta(days=365)
        v = self.verify(self.now - ttl + timedelta(seconds=1))
        self.assertTrue(campus.is_campus_verified(self.user, self.now))
        # One second later the same unrevoked row no longer counts: nothing closed it.
        self.assertFalse(campus.is_campus_verified(self.user, self.now + timedelta(seconds=2)))
        v.refresh_from_db()
        self.assertIsNone(v.revoked_at)

    def test_revoked_rows_do_not_count_and_no_row_does_not_count(self):
        self.assertFalse(campus.is_campus_verified(self.user))
        self.verify(self.now, revoked_at=self.now, revoke_reason="LEFT_CAMPUS")
        self.assertFalse(campus.is_campus_verified(self.user))

    def test_it_is_per_user(self):
        other = User.objects.create_user("o", "o@lpu.co.in", PW)
        self.verify(self.now)
        self.assertFalse(campus.is_campus_verified(other))


@override_settings(SITE_URL="https://bb.example.org")
class ConfirmCreatesVerificationTests(TestCase):
    def signup(self, name, email):
        user = User.objects.create_user(name, email, PW)
        return user, *issue_token(self, user)

    def test_campus_email_confirm_verifies_binds_identity_and_audits(self):
        user, uid, token = self.signup("stu", "stu@lpu.co.in")
        confirm_email(uid, token, PW, f"{REG[:4]}-{REG[4:]}")
        v = CampusVerification.objects.get(user=user)
        self.assertEqual((v.method, v.verified_email, v.verified_by_id), ("CAMPUS_EMAIL", "stu@lpu.co.in", None))
        self.assertTrue(campus.is_campus_verified(user))
        ident = StudentIdentity.objects.get(user=user)
        self.assertEqual(ident.reg_no_hmac, hash_reg_no(REG)[0])
        self.assertEqual((ident.reg_no_last4, ident.source), ("5678", "SELF_DECLARED"))
        self.assertEqual(AuditEvent.objects.get(subject=user).action, "VERIFIED")
        self.assertFalse(IdentityReview.objects.exists())

    def test_raw_reg_no_is_stored_nowhere(self):
        user, uid, token = self.signup("stu", "stu@lpu.co.in")
        confirm_email(uid, token, PW, REG)
        for model in (StudentIdentity, CampusVerification, IdentityReview, AuditEvent):
            for row in model.objects.all().values():
                self.assertNotIn(REG, " ".join(str(v) for v in row.values()), model)

    def test_non_campus_address_is_email_verified_only(self):
        user, uid, token = self.signup("stu", "stu@gmail.com")
        confirm_email(uid, token, PW, REG)  # a reg no here is ignored, not bound
        user.refresh_from_db()
        self.assertIsNotNone(user.email_verified_at)
        self.assertFalse(CampusVerification.objects.exists())
        self.assertFalse(StudentIdentity.objects.exists())

    def test_lookalike_domain_is_not_campus_verified(self):
        user, uid, token = self.signup("stu", "stu@notlpu.co.in")
        confirm_email(uid, token, PW)
        self.assertFalse(campus.is_campus_verified(user))

    def test_missing_or_bad_reg_no_writes_nothing_and_keeps_the_token(self):
        user, uid, token = self.signup("stu", "stu@lpu.co.in")
        for bad in ["", "123", "abcdefgh"]:
            with self.subTest(bad=bad), self.assertRaises(InvalidRegNo):
                confirm_email(uid, token, PW, bad)
        user.refresh_from_db()
        self.assertIsNone(user.email_verified_at)
        self.assertIsNotNone(user.email_token_hash)
        self.assertFalse(CampusVerification.objects.exists())
        confirm_email(uid, token, PW, REG)  # the same link still works
        self.assertTrue(campus.is_campus_verified(user))

    def test_bad_reg_no_with_wrong_password_is_just_invalid_token(self):
        """The reg-no check must not be a way to probe an account without its password."""
        _, uid, token = self.signup("stu", "stu@lpu.co.in")
        with self.assertRaises(InvalidToken):
            confirm_email(uid, token, "wrong-password-1", "")

    def test_collision_verifies_the_second_user_unbound_and_opens_one_review(self):
        first, uid1, t1 = self.signup("first", "first@lpu.co.in")
        confirm_email(uid1, t1, PW, REG)
        second, uid2, t2 = self.signup("second", "second@lpu.co.in")
        confirm_email(uid2, t2, PW, REG)  # same reg no: must not raise, must not lose the verification

        self.assertTrue(campus.is_campus_verified(second))
        self.assertFalse(StudentIdentity.objects.filter(user=second).exists())
        self.assertEqual(StudentIdentity.objects.get().user, first)  # first binding unchanged
        review = IdentityReview.objects.get()
        self.assertEqual((review.claimant, review.status), (second, "OPEN"))
        self.assertEqual(review.existing_identity.user, first)
        self.assertEqual(review.claimed_last4, "5678")
        second.refresh_from_db()
        self.assertIsNotNone(second.email_verified_at)

    def test_repeating_a_colliding_verification_does_not_open_a_second_review(self):
        first, uid1, t1 = self.signup("first", "first@lpu.co.in")
        confirm_email(uid1, t1, PW, REG)
        second = User.objects.create_user("second", "second@lpu.co.in", PW)
        campus.verify_campus_email(second, REG)
        campus.verify_campus_email(second, REG)
        self.assertEqual(IdentityReview.objects.filter(claimant=second, status="OPEN").count(), 1)
        self.assertEqual(CampusVerification.objects.filter(user=second, revoked_at__isnull=True).count(), 1)

    def test_renewal_needs_no_reg_no_and_supersedes_the_old_row(self):
        user, uid, token = self.signup("stu", "stu@lpu.co.in")
        confirm_email(uid, token, PW, REG)
        old = CampusVerification.objects.get(user=user)
        self.assertFalse(campus.needs_reg_no(user))
        campus.verify_campus_email(user, "")
        old.refresh_from_db()
        self.assertEqual(old.revoke_reason, "SUPERSEDED")
        self.assertEqual(CampusVerification.objects.filter(user=user, revoked_at__isnull=True).count(), 1)
        self.assertEqual(StudentIdentity.objects.filter(user=user).count(), 1)

    def test_a_lapsed_but_unswept_row_does_not_block_renewal(self):
        user, uid, token = self.signup("stu", "stu@lpu.co.in")
        confirm_email(uid, token, PW, REG)
        CampusVerification.objects.filter(user=user).update(verified_at=timezone.now() - timedelta(days=400))
        self.assertFalse(campus.is_campus_verified(user))
        campus.verify_campus_email(user, "")
        self.assertTrue(campus.is_campus_verified(user))


@override_settings(SITE_URL="https://bb.example.org")
class ConfirmPageRegNoTests(TestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)

    def page(self, email):
        user = User.objects.create_user("stu", email, PW)
        uid, token = issue_token(self, user)
        return user, reverse("accounts:verify_email", args=[uid, token])

    def test_campus_link_asks_for_the_reg_no_and_other_links_do_not(self):
        _, url = self.page("stu@lpu.co.in")
        self.assertContains(self.client.get(url), 'name="reg_no"')
        User.objects.all().delete()
        _, url = self.page("stu@gmail.com")
        self.assertNotContains(self.client.get(url), 'name="reg_no"')

    def test_bad_reg_no_shows_a_field_error_without_spending_the_login_budget(self):
        user, url = self.page("stu@lpu.co.in")
        with limits(login_failures_per_account="2/10m"):
            for _ in range(5):
                r = self.client.post(url, {"password": PW, "reg_no": "nope"})
                self.assertEqual(r.status_code, 200)
                self.assertContains(r, "Enter a valid registration number.")
            r = self.client.post(url, {"password": PW, "reg_no": REG})
        self.assertEqual(r.status_code, 302)
        user.refresh_from_db()
        self.assertIsNotNone(user.email_verified_at)
        self.assertTrue(campus.is_campus_verified(user))

    def test_success_logs_the_user_in(self):
        _, url = self.page("stu@lpu.co.in")
        self.client.post(url, {"password": PW, "reg_no": REG})
        self.assertIn("_auth_user_id", self.client.session)
