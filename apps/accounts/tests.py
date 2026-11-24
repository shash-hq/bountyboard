from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.core import mail
from django.test import TestCase
from django.urls import reverse

from .testing import create_verified_user

User = get_user_model()


class AccountsAuthTests(TestCase):
    def setUp(self):
        self.user = create_verified_user(
            username="testuser",
            email="testuser@campus.edu.in",
            password="StrongPassword123!",
        )

    def test_signup_creates_unverified_user_emails_a_link_and_does_not_log_in(self):
        """Signup proves nothing about the address, so it must not log the user in."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                reverse("accounts:signup"),
                {
                    "username": "newstudent",
                    "email": "newstudent@campus.edu.in",
                    "password": "SecretPassword123!",
                    "password_confirm": "SecretPassword123!",
                },
                follow=True,
            )
        self.assertRedirects(response, reverse("accounts:login"))
        user = User.objects.get(username="newstudent")
        self.assertIsNone(user.email_verified_at)
        self.assertFalse(response.context["user"].is_authenticated)
        self.assertEqual([m.to for m in mail.outbox], [["newstudent@campus.edu.in"]])
        self.assertIn("/accounts/verify-email/", mail.outbox[0].body)

    def test_full_signup_confirm_login_flow(self):
        import re
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(
                reverse("accounts:signup"),
                {
                    "username": "newstudent",
                    "email": "newstudent@campus.edu.in",
                    "password": "SecretPassword123!",
                    "password_confirm": "SecretPassword123!",
                },
            )
        uid, token = re.search(r"/verify-email/(\d+)/([\w-]+)/", mail.outbox[0].body).groups()
        response = self.client.post(
            reverse("accounts:verify_email", args=[uid, token]), {"password": "SecretPassword123!"}, follow=True
        )
        self.assertRedirects(response, reverse("board"))
        self.assertEqual(response.context["user"].username, "newstudent")

    def test_signup_duplicate_username_denial(self):
        """Verify signup is denied if username already exists."""
        response = self.client.post(
            reverse("accounts:signup"),
            {
                "username": "testuser",  # Duplicate username
                "email": "unique@campus.edu.in",
                "password": "SecretPassword123!",
                "password_confirm": "SecretPassword123!",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertFormError(
            response.context["form"],
            "username",
            "This username is already taken. Pick a different handle.",
        )

    def test_signup_is_open_to_any_email_domain(self):
        """The old domain gate is gone: any address can sign up, then must confirm it by link."""
        for name, email in [("gmailer", "gmailer@gmail.com"), ("lookalike", "x@notlpu.co.in")]:
            with self.subTest(email=email):
                response = self.client.post(
                    reverse("accounts:signup"),
                    {
                        "username": name,
                        "email": email,
                        "password": "SecretPassword123!",
                        "password_confirm": "SecretPassword123!",
                    },
                )
                self.assertEqual(response.status_code, 302)
                user = User.objects.get(username=name)
                self.assertIsNone(user.email_verified_at)  # not logged in, not verified

    def test_login_with_username_and_email(self):
        """Verify login works with both username and email."""
        # Login with username
        resp1 = self.client.post(
            reverse("accounts:login"),
            {"username_or_email": "testuser", "password": "StrongPassword123!"},
            follow=True,
        )
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(resp1.context["user"].username, "testuser")

        self.client.logout()

        # Login with email
        resp2 = self.client.post(
            reverse("accounts:login"),
            {"username_or_email": "testuser@campus.edu.in", "password": "StrongPassword123!"},
            follow=True,
        )
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.context["user"].username, "testuser")

    def test_login_invalid_credentials(self):
        """Verify invalid login displays error."""
        response = self.client.post(
            reverse("accounts:login"),
            {"username_or_email": "testuser", "password": "WrongPassword!"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Invalid username/email or password.")

    def test_unverified_user_with_correct_password_is_refused_with_resend_option(self):
        User.objects.create_user("pending", "pending@campus.edu.in", "StrongPassword123!")
        response = self.client.post(
            reverse("accounts:login"),
            {"username_or_email": "pending", "password": "StrongPassword123!"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.wsgi_request.user.is_authenticated)
        self.assertContains(response, "Confirm your email first")
        self.assertContains(response, reverse("accounts:resend_verification"))

    def test_unverified_status_is_not_revealed_without_the_password(self):
        User.objects.create_user("pending", "pending@campus.edu.in", "StrongPassword123!")
        response = self.client.post(
            reverse("accounts:login"),
            {"username_or_email": "pending", "password": "WrongPassword!"},
        )
        self.assertContains(response, "Invalid username/email or password.")
        self.assertNotContains(response, "Confirm your email first")

    def test_resend_sends_new_link_to_unverified_account_only(self):
        User.objects.create_user("pending", "pending@campus.edu.in", "StrongPassword123!")
        for email, expected in [("PENDING@campus.edu.in", 1), ("testuser@campus.edu.in", 0), ("ghost@campus.edu.in", 0)]:
            mail.outbox.clear()
            with self.captureOnCommitCallbacks(execute=True):
                response = self.client.post(reverse("accounts:resend_verification"), {"email": email}, follow=True)
            self.assertRedirects(response, reverse("accounts:login"))
            self.assertEqual(len(mail.outbox), expected, email)
            # identical wording whatever the outcome: no account enumeration
            self.assertContains(response, "If that address belongs to an account that still needs confirming")

    def test_logout_post_only(self):
        """Verify logout works via POST and redirects to home."""
        self.client.force_login(self.user)
        response = self.client.post(reverse("accounts:logout"), follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["user"].is_authenticated)

    def test_me_requires_login(self):
        """Verify unauthenticated access to /me/ redirects to login with next param."""
        response = self.client.get(reverse("me"))
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse("accounts:login"), response.url)
        self.assertIn("next=/me/", response.url)

    def test_me_authenticated(self):
        """Verify /me/ renders user dashboard when logged in."""
        self.client.force_login(self.user)
        response = self.client.get(reverse("me"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TESTUSER")

    def test_moderators_group_created(self):
        """Verify the 'Moderators' group exists and has moderate permission."""
        group = Group.objects.filter(name="Moderators").first()
        self.assertIsNotNone(group)
        self.assertTrue(group.permissions.filter(codename="moderate").exists())

    def test_password_reset_flow(self):
        """Verify password reset request sends email in test outbox."""
        response = self.client.post(
            reverse("accounts:password_reset"),
            {"email": "testuser@campus.edu.in"},
            follow=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(mail.outbox), 1)
        self.assertIn("Reset your account password", mail.outbox[0].subject)
