import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .services import InvalidToken, confirm_email, peek_confirmation, send_verification_email
from .tests_ratelimits import limits

User = get_user_model()
LINK = re.compile(r"/accounts/verify-email/(\d+)/([\w-]+)/")
PW = "pw-Str0ng-pass"


def issue_token(testcase, user):
    """Send a verification email and return (uid, raw token) from its link."""
    mail.outbox.clear()
    with testcase.captureOnCommitCallbacks(execute=True):
        send_verification_email(user.pk)
    uid, token = LINK.search(mail.outbox[-1].body).groups()
    return int(uid), token


@override_settings(SITE_URL="https://bb.example.org")
class TokenServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("newbie", "newbie@example.org", PW)

    def test_email_has_absolute_link_names_the_address_and_only_the_hash_is_stored(self):
        uid, token = issue_token(self, self.user)
        body = mail.outbox[0].body
        self.assertEqual(mail.outbox[0].to, ["newbie@example.org"])
        self.assertIn(f"https://bb.example.org/accounts/verify-email/{uid}/{token}/", body)
        self.assertIn("newbie@example.org", body)
        self.assertIn("Didn't sign up? Ignore this email", body)
        self.user.refresh_from_db()
        self.assertNotIn(token, self.user.email_token_hash)
        self.assertEqual(len(self.user.email_token_hash), 64)

    def test_token_lasts_24_hours(self):
        before = timezone.now()
        issue_token(self, self.user)
        self.user.refresh_from_db()
        self.assertAlmostEqual((self.user.email_token_expires_at - before).total_seconds(), 24 * 3600, delta=5)

    def test_confirm_marks_verified_and_is_single_use(self):
        uid, token = issue_token(self, self.user)
        confirm_email(uid, token, PW)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.email_verified_at)
        self.assertIsNone(self.user.email_token_hash)
        with self.assertRaises(InvalidToken):
            confirm_email(uid, token, PW)

    def test_wrong_password_is_refused_and_does_not_burn_the_link(self):
        uid, token = issue_token(self, self.user)
        with self.assertRaises(InvalidToken):
            confirm_email(uid, token, "not-the-password")
        self.user.refresh_from_db()
        self.assertIsNone(self.user.email_verified_at)
        confirm_email(uid, token, PW)  # the owner can still use the link

    def test_resend_kills_the_previous_token(self):
        uid, old = issue_token(self, self.user)
        _, new = issue_token(self, self.user)
        self.assertNotEqual(old, new)
        with self.assertRaises(InvalidToken):
            confirm_email(uid, old, PW)
        confirm_email(uid, new, PW)

    def test_changing_the_password_kills_every_older_link(self):
        """The token is bound to the password hash (like PasswordResetTokenGenerator)."""
        uid, token = issue_token(self, self.user)
        self.user.refresh_from_db()  # a stale instance would write the NULL token back and pass trivially
        self.user.set_password("a-different-Pass-99")
        self.user.save(update_fields=["password"])
        self.assertIsNone(peek_confirmation(uid, token))
        with self.assertRaises(InvalidToken):
            confirm_email(uid, token, "a-different-Pass-99")

    def test_a_token_is_bound_to_its_user_id(self):
        other = User.objects.create_user("other", "other@example.org", PW)
        _, token = issue_token(self, self.user)
        with self.assertRaises(InvalidToken):
            confirm_email(other.pk, token, PW)

    def test_expired_token_is_rejected_at_ttl_and_valid_just_before(self):
        uid, token = issue_token(self, self.user)
        self.user.refresh_from_db()
        expiry = self.user.email_token_expires_at
        with mock.patch("apps.accounts.services.timezone.now", return_value=expiry):
            with self.assertRaises(InvalidToken):
                confirm_email(uid, token, PW)
        with mock.patch("apps.accounts.services.timezone.now", return_value=expiry - timedelta(seconds=1)):
            confirm_email(uid, token, PW)

    def test_unknown_user_or_token_is_rejected(self):
        uid, _ = issue_token(self, self.user)
        for args in [(uid, "not-a-real-token"), (999999, "x")]:
            with self.assertRaises(InvalidToken):
                confirm_email(*args, PW)

    def test_already_verified_user_gets_no_email_and_no_new_token(self):
        self.user.email_verified_at = timezone.now()
        self.user.save()
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.assertFalse(send_verification_email(self.user.pk))
        self.assertEqual(mail.outbox, [])
        self.user.refresh_from_db()
        self.assertIsNone(self.user.email_token_hash)

    def test_rolled_back_transaction_sends_nothing(self):
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            try:
                with transaction.atomic():
                    send_verification_email(self.user.pk)
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
        self.assertEqual(callbacks, [])
        self.assertEqual(mail.outbox, [])


class ConfirmPageTests(TestCase):
    """The pre-hijacking scenario: an attacker signs up with the victim's address and their own
    password, then the victim receives the unsolicited link."""

    def setUp(self):
        cache.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("accounts:signup"), {
                "username": "attacker", "email": "victim@example.org",
                "password": "Attacker-pass-123", "password_confirm": "Attacker-pass-123",
            })
        self.user = User.objects.get(email="victim@example.org")
        self.uid, self.token = [LINK.search(mail.outbox[-1].body).group(i) for i in (1, 2)]
        self.url = reverse("accounts:verify_email", args=[self.uid, self.token])
        self.victim = self.client_class()  # the victim's own browser

    def test_get_renders_the_password_form_with_the_address_and_never_verifies(self):
        for _ in range(3):  # link scanners prefetch; repeats must be harmless
            response = self.victim.get(self.url)
            self.assertContains(response, "victim@example.org")
            self.assertContains(response, 'name="password"')
            self.assertContains(response, "Didn't sign up?")
        self.user.refresh_from_db()
        self.assertIsNone(self.user.email_verified_at)
        self.assertNotIn("_auth_user_id", self.victim.session)

    def test_victim_without_the_attackers_password_cannot_verify(self):
        for guess in ["victim-own-password", "", "Attacker-pass-12"]:
            response = self.victim.post(self.url, {"password": guess})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Check your password")
        self.user.refresh_from_db()
        self.assertIsNone(self.user.email_verified_at)
        self.assertNotIn("_auth_user_id", self.victim.session)

    def test_correct_password_verifies_logs_in_and_rotates_the_session(self):
        session = self.victim.session
        session["anon"] = 1
        session.save()
        before = self.victim.session.session_key
        response = self.victim.post(self.url, {"password": "Attacker-pass-123"})
        self.assertRedirects(response, reverse("board"))
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.email_verified_at)
        self.assertEqual(int(self.victim.session["_auth_user_id"]), self.user.pk)
        self.assertNotEqual(self.victim.session.session_key, before)

    def test_used_or_garbage_links_are_400(self):
        self.victim.post(self.url, {"password": "Attacker-pass-123"})
        self.assertEqual(self.client_class().get(self.url).status_code, 400)
        self.assertEqual(self.client_class().post(self.url, {"password": "x"}).status_code, 400)
        garbage = reverse("accounts:verify_email", args=[self.uid, "garbage"])
        self.assertEqual(self.client_class().get(garbage).status_code, 400)
        self.assertNotContains(self.client_class().get(garbage), "victim@example.org", status_code=400)

    def test_forgotten_password_has_a_way_out_via_signup_with_the_email_prefilled(self):
        response = self.victim.get(self.url)
        self.assertContains(response, reverse("accounts:signup") + "?email=victim%40example.org")
        self.assertContains(self.victim.get(reverse("accounts:signup") + "?email=victim%40example.org"),
                            'value="victim@example.org"')

    @limits(login_failures_per_account="3/h")
    def test_wrong_passwords_hit_the_login_throttle_and_even_the_right_one_is_then_refused(self):
        codes = [self.victim.post(self.url, {"password": f"wrong-{i}"}).status_code for i in range(6)]
        self.assertEqual(codes[:4], [200] * 4)  # 3 allowed failures; the 4th still gets an answer
        self.assertEqual(set(codes[4:]), {429})
        self.assertEqual(self.victim.post(self.url, {"password": "Attacker-pass-123"}).status_code, 429)
        self.user.refresh_from_db()
        self.assertIsNone(self.user.email_verified_at)

    @limits(login_failures_per_account="3/h")
    def test_failed_logins_and_failed_confirms_share_one_account_budget(self):
        """Confirm must not be a way around login's per-account limit, nor the reverse."""
        for i in range(4):
            self.client.post(reverse("accounts:login"), {"username_or_email": "attacker", "password": f"bad-{i}"})
        self.assertEqual(self.victim.post(self.url, {"password": "Attacker-pass-123"}).status_code, 429)

    def _fail(self, how, i=0):
        """One wrong-password attempt at the SAME account, named a different way each time."""
        login = reverse("accounts:login")
        if how == "login-username":
            return self.client.post(login, {"username_or_email": "attacker", "password": f"bad-{i}"})
        if how == "login-email":
            return self.client.post(login, {"username_or_email": " Victim@Example.ORG ", "password": f"bad-{i}"})
        return self.victim.post(self.url, {"password": f"bad-{i}"})

    @limits(login_failures_per_account="3/h")
    def test_failures_via_login_username_login_email_and_confirm_spend_one_budget(self):
        codes = [self._fail(how, i).status_code
                 for i, how in enumerate(["login-username", "login-email", "confirm", "login-email"])]
        self.assertEqual(codes, [200] * 4)  # 3 allowed failures + the 4th still answered
        for how in ("login-username", "login-email", "confirm"):
            self.assertEqual(self._fail(how).status_code, 429, how)

    @limits(login_failures_per_account="3/h")
    def test_the_limit_is_reached_at_the_same_threshold_whichever_view_is_used(self):
        orders = {
            "login-username": ["login-username"] * 8,
            "login-email": ["login-email"] * 8,
            "confirm": ["confirm"] * 8,
            "mixed": ["confirm", "login-email", "login-username", "confirm", "login-email"] * 2,
        }
        for name, hows in orders.items():
            with self.subTest(name):
                cache.clear()
                codes = [self._fail(how, i).status_code for i, how in enumerate(hows)]
                self.assertEqual(codes.index(429), 4)
                self.assertEqual(set(codes[4:]), {429})

    @limits(login_failures_per_account="3/h")
    def test_unknown_identifiers_are_limited_at_the_same_threshold_as_real_accounts(self):
        login = reverse("accounts:login")
        names = ["nobody", "NOBODY", " nobody ", "nobody", "nobody", "nobody"]
        codes = [self.client.post(login, {"username_or_email": n, "password": "x"}).status_code for n in names]
        self.assertEqual(codes.index(429), 4)  # same as a real account: indistinguishable
        other = self.client.post(login, {"username_or_email": "someone-else", "password": "x"})
        self.assertEqual(other.status_code, 200)  # a different unknown name has its own bucket

    @limits(login_per_ip="2/h")
    def test_confirm_shares_the_per_ip_budget_with_login(self):
        for i in range(3):
            self.client.post(reverse("accounts:login"), {"username_or_email": "x", "password": "y"}, REMOTE_ADDR="10.9.9.9")
        response = self.victim.post(self.url, {"password": "Attacker-pass-123"}, REMOTE_ADDR="10.9.9.9")
        self.assertEqual(response.status_code, 429)


class VerifiedEmailMiddlewareTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("member", "member@example.org", PW, email_verified_at=timezone.now())
        self.me = reverse("me")

    def test_verified_user_stays_logged_in(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.me).status_code, 200)

    def test_user_who_loses_verification_mid_session_is_logged_out(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.me).status_code, 200)
        User.objects.filter(pk=self.user.pk).update(email_verified_at=None)
        self.assertEqual(self.client.get(self.me).status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_session_of_a_never_verified_user_is_rejected(self):
        unverified = User.objects.create_user("ghost", "ghost@example.org", PW)
        self.client.force_login(unverified)
        self.assertEqual(self.client.get(self.me).status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertContains(self.client.get(reverse("accounts:login")), "Confirm your email to continue")
