import re
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.db import transaction
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .services import InvalidToken, confirm_email, send_verification_email

User = get_user_model()
TOKEN_IN_LINK = re.compile(r"/accounts/verify-email/([\w-]+)/")


def issue_token(testcase, user):
    """Send a verification email and return the raw token from its link."""
    mail.outbox.clear()
    with testcase.captureOnCommitCallbacks(execute=True):
        send_verification_email(user.pk)
    return TOKEN_IN_LINK.search(mail.outbox[-1].body).group(1)


@override_settings(SITE_URL="https://bb.example.org")
class TokenServiceTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("newbie", "newbie@example.org", "pw-Str0ng-pass")

    def test_email_contains_absolute_link_and_only_the_hash_is_stored(self):
        token = issue_token(self, self.user)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["newbie@example.org"])
        self.assertIn(f"https://bb.example.org/accounts/verify-email/{token}/", mail.outbox[0].body)
        self.user.refresh_from_db()
        self.assertNotIn(token, self.user.email_token_hash)
        self.assertEqual(len(self.user.email_token_hash), 64)

    def test_token_lasts_24_hours(self):
        before = timezone.now()
        issue_token(self, self.user)
        self.user.refresh_from_db()
        ttl = self.user.email_token_expires_at - before
        self.assertAlmostEqual(ttl.total_seconds(), 24 * 3600, delta=5)

    def test_confirm_marks_verified_and_is_single_use(self):
        token = issue_token(self, self.user)
        confirm_email(token)
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.email_verified_at)
        self.assertIsNone(self.user.email_token_hash)
        self.assertIsNone(self.user.email_token_expires_at)
        with self.assertRaises(InvalidToken):
            confirm_email(token)

    def test_resend_kills_the_previous_token(self):
        old = issue_token(self, self.user)
        new = issue_token(self, self.user)
        self.assertNotEqual(old, new)
        with self.assertRaises(InvalidToken):
            confirm_email(old)
        confirm_email(new)  # the newest one still works

    def test_expired_token_is_rejected_at_ttl_and_valid_just_before(self):
        token = issue_token(self, self.user)
        self.user.refresh_from_db()
        expiry = self.user.email_token_expires_at
        with mock.patch("apps.accounts.services.timezone.now", return_value=expiry):
            with self.assertRaises(InvalidToken):  # boundary is exclusive: at expiry it is dead
                confirm_email(token)
        with mock.patch("apps.accounts.services.timezone.now", return_value=expiry - timedelta(seconds=1)):
            confirm_email(token)

    def test_unknown_token_is_rejected(self):
        with self.assertRaises(InvalidToken):
            confirm_email("not-a-real-token")

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
        """The email goes out on commit: a token that was never saved must never be mailed."""
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

    def test_confirm_does_not_change_an_existing_verified_timestamp(self):
        token = issue_token(self, self.user)
        earlier = timezone.now() - timedelta(days=3)
        User.objects.filter(pk=self.user.pk).update(email_verified_at=earlier)
        confirm_email(token)
        self.user.refresh_from_db()
        self.assertEqual(self.user.email_verified_at, earlier)


class ConfirmPageTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("newbie", "newbie@example.org", "pw-Str0ng-pass")
        self.token = issue_token(self, self.user)
        self.url = reverse("accounts:verify_email", args=[self.token])

    def test_get_shows_a_button_and_does_not_consume_the_token(self):
        """Link scanners prefetch GETs; only the human's POST may confirm."""
        for _ in range(3):
            response = self.client.get(self.url)
            self.assertContains(response, "CONFIRM EMAIL")
        self.user.refresh_from_db()
        self.assertIsNone(self.user.email_verified_at)
        self.assertIsNotNone(self.user.email_token_hash)

    def test_post_confirms_then_redirects_to_login(self):
        response = self.client.post(self.url)
        self.assertRedirects(response, reverse("accounts:login"))
        self.user.refresh_from_db()
        self.assertIsNotNone(self.user.email_verified_at)

    def test_post_with_used_token_is_400_and_leaves_state_alone(self):
        self.client.post(self.url)
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, 400)
        self.assertContains(response, "DOESN'T WORK", status_code=400)

    def test_post_with_garbage_token_is_400(self):
        response = self.client.post(reverse("accounts:verify_email", args=["garbage"]))
        self.assertEqual(response.status_code, 400)


class VerifiedEmailMiddlewareTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            "member", "member@example.org", "pw-Str0ng-pass", email_verified_at=timezone.now()
        )
        self.me = reverse("me")

    def test_verified_user_stays_logged_in(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.me).status_code, 200)

    def test_user_who_loses_verification_mid_session_is_logged_out(self):
        """e.g. a later change_email: the existing session must not outlive the verification."""
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(self.me).status_code, 200)
        User.objects.filter(pk=self.user.pk).update(email_verified_at=None)
        response = self.client.get(self.me)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_session_of_a_never_verified_user_is_rejected(self):
        unverified = User.objects.create_user("ghost", "ghost@example.org", "pw-Str0ng-pass")
        self.client.force_login(unverified)
        response = self.client.get(self.me)
        self.assertEqual(response.status_code, 302)
        self.assertNotIn("_auth_user_id", self.client.session)
        follow = self.client.get(reverse("accounts:login"))
        self.assertContains(follow, "Confirm your email to continue")
