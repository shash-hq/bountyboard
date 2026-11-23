"""Signup: replacing unverified accounts, protecting verified ones, no enumeration, budgets, purge, races."""
import threading
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.contrib.messages import get_messages
from django.core import mail
from django.core.cache import cache
from django.core.management import call_command
from django.db import IntegrityError, connection
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.bounties.models import Bounty

from . import services
from .services import confirm_email, peek_confirmation
from .tests_email_verification import LINK
from .tests_ratelimits import limits
from .testing import create_verified_user

User = get_user_model()
PW1, PW2 = "First-pass-12345", "Second-pass-67890"


class SignupBase(TestCase):
    def setUp(self):
        cache.clear()

    def signup(self, email, username="someone", password=PW1, **extra):
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("accounts:signup"), {
                "username": username, "email": email, "password": password, "password_confirm": password,
            }, **extra)

    def link(self):
        uid, token = LINK.search(mail.outbox[-1].body).groups()
        return int(uid), token

    def age(self, user, hours=25):
        User.objects.filter(pk=user.pk).update(date_joined=timezone.now() - timedelta(hours=hours))


class ReplacementTests(SignupBase):
    def test_second_signup_on_an_unverified_address_replaces_the_first(self):
        self.signup("victim@example.org", "attacker", PW1)
        old_uid, old_token = self.link()
        self.signup("victim@example.org", "victim", PW2)
        new_uid, new_token = self.link()

        users = User.objects.filter(email__iexact="victim@example.org")
        self.assertEqual([u.username for u in users], ["victim"])
        self.assertNotEqual(old_uid, new_uid)
        # the first link is dead, and so is the first password
        self.assertIsNone(peek_confirmation(old_uid, old_token))
        self.assertEqual(self.client.get(reverse("accounts:verify_email", args=[old_uid, old_token])).status_code, 400)
        with self.assertRaises(services.InvalidToken):
            confirm_email(new_uid, new_token, PW1)
        # the second link plus the second password works
        confirm_email(new_uid, new_token, PW2)
        self.assertIsNotNone(User.objects.get(pk=new_uid).email_verified_at)

    def test_same_person_retrying_with_the_same_username_is_fine(self):
        self.signup("me@example.org", "me_ece", PW1)
        response = self.signup("me@example.org", "me_ece", PW2)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(User.objects.filter(username="me_ece").count(), 1)

    def test_attacker_cannot_squat_an_address_by_signing_up_first(self):
        self.signup("victim@example.org", "squatter", PW1)
        self.signup("victim@example.org", "realowner", PW2)  # the real owner is not blocked
        self.assertEqual(User.objects.get(email="victim@example.org").username, "realowner")

    def test_a_fresh_unverified_account_holds_its_username_against_other_addresses(self):
        self.signup("a@example.org", "popular", PW1)
        response = self.signup("b@example.org", "popular", PW2)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "already taken")
        self.assertEqual(User.objects.filter(username="popular").count(), 1)
        self.assertEqual(User.objects.get(username="popular").email, "a@example.org")


class VerifiedAccountTests(SignupBase):
    def setUp(self):
        super().setUp()
        self.owner = create_verified_user("owner", "owner@example.org", PW1)

    def messages(self, response, email):
        # the client's session keeps earlier flash messages too, so compare the latest only
        return [m.message.replace(email, "<EMAIL>") for m in get_messages(response.wsgi_request)][-1]

    def test_signup_never_replaces_a_verified_account_and_looks_identical(self):
        existing = self.signup("Owner@Example.ORG", "intruder", PW2)
        new = self.signup("fresh@example.org", "fresh", PW2)
        self.assertEqual(existing.status_code, new.status_code)
        self.assertEqual(existing["Location"], new["Location"])
        self.assertEqual(self.messages(existing, "owner@example.org"), self.messages(new, "fresh@example.org"))

        self.owner.refresh_from_db()  # untouched
        self.assertEqual(self.owner.username, "owner")
        self.assertTrue(self.owner.check_password(PW1))
        self.assertIsNotNone(self.owner.email_verified_at)
        self.assertFalse(User.objects.filter(username="intruder").exists())

    def test_the_owner_gets_a_you_already_have_an_account_email_not_a_confirm_link(self):
        self.signup("owner@example.org", "intruder", PW2)
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["owner@example.org"])
        self.assertIn("already have an account", message.subject)
        self.assertNotIn("verify-email", message.body)
        self.assertIn(reverse("accounts:login"), message.body)
        self.assertIn(reverse("accounts:password_reset"), message.body)

    def test_a_new_address_gets_the_confirm_link(self):
        self.signup("fresh@example.org", "fresh", PW2)
        self.assertIn("verify-email", mail.outbox[0].body)

    def test_an_account_that_owns_rows_is_never_replaced_and_it_is_logged_as_an_error(self):
        ghost = User.objects.create_user("ghost", "ghost@example.org", PW1)  # unverified but owns a bounty
        Bounty.objects.create(poster=ghost, title="Odd bounty", description="x" * 30, reward=50,
                              deadline=timezone.now() + timedelta(days=2))
        with self.assertLogs("apps.accounts.services", "ERROR"):
            response = self.signup("ghost@example.org", "usurper", PW2)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(User.objects.filter(pk=ghost.pk).exists())
        self.assertFalse(User.objects.filter(username="usurper").exists())
        self.assertNotIn("verify-email", mail.outbox[0].body)


class EmailBudgetTests(SignupBase):
    @limits(verify_resend_per_email="2/h")
    def test_signup_and_resend_share_one_per_address_budget(self):
        self.signup("target@example.org", "t1", PW1)
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            self.client.post(reverse("accounts:resend_verification"), {"email": "target@example.org"})
        sent = len(mail.outbox)
        mail.outbox.clear()
        response = self.signup("target@example.org", "t2", PW2)
        self.assertEqual(response.status_code, 429)
        self.assertEqual(mail.outbox, [])
        self.assertEqual(sent, 1)  # the resend's mail; the refused signup sent nothing

    @limits(verify_resend_per_email="2/h")
    def test_the_budget_also_covers_a_verified_owner_being_mailed(self):
        create_verified_user("owner", "owner@example.org", PW1)
        codes = []
        for i in range(4):
            codes.append(self.signup("owner@example.org", f"x{i}", PW2).status_code)
        self.assertEqual(codes, [302, 302, 429, 429])

    @limits(verify_resend_per_email="2/h")
    def test_invalid_signups_do_not_burn_the_budget(self):
        for _ in range(4):
            self.client.post(reverse("accounts:signup"), {
                "username": "bad", "email": "target@example.org", "password": PW1, "password_confirm": "mismatch"})
        self.assertEqual(self.signup("target@example.org", "bad", PW1).status_code, 302)


class PurgeTests(SignupBase):
    def test_command_purges_stale_unverified_accounts_and_frees_their_username(self):
        stale = User.objects.create_user("abandoned", "abandoned@example.org", PW1)
        self.age(stale)
        fresh = User.objects.create_user("justnow", "justnow@example.org", PW1)
        live_link = User.objects.create_user("resending", "resending@example.org", PW1)
        self.age(live_link)
        with self.captureOnCommitCallbacks(execute=True):
            services.send_verification_email(live_link.pk)
        owns = User.objects.create_user("owns", "owns@example.org", PW1)
        self.age(owns)
        Bounty.objects.create(poster=owns, title="Mine bounty", description="x" * 30, reward=50,
                              deadline=timezone.now() + timedelta(days=2))
        staff = User.objects.create_user("staffer", "staffer@example.org", PW1, is_staff=True)
        self.age(staff)
        grouped = User.objects.create_user("grouped", "grouped@example.org", PW1)
        grouped.groups.add(Group.objects.get(name="Verifiers"))
        self.age(grouped)
        verified = create_verified_user("verified", "verified@example.org", PW1)
        self.age(verified)

        call_command("purge_unverified_accounts", stdout=mock.MagicMock())

        remaining = set(User.objects.values_list("username", flat=True))
        self.assertNotIn("abandoned", remaining)
        self.assertEqual(remaining, {"justnow", "resending", "owns", "staffer", "grouped", "verified"})
        self.assertEqual(self.signup("new@example.org", "abandoned", PW2).status_code, 302)

    def test_a_signup_lazily_purges_a_stale_holder_of_the_username(self):
        stale = User.objects.create_user("squat", "old@example.org", PW1)
        self.age(stale)
        self.assertEqual(self.signup("new@example.org", "squat", PW2).status_code, 302)
        self.assertEqual(User.objects.get(username="squat").email, "new@example.org")
        self.assertFalse(User.objects.filter(pk=stale.pk).exists())

    def test_a_stale_holder_that_owns_rows_still_blocks_the_username(self):
        stale = User.objects.create_user("squat", "old@example.org", PW1)
        Bounty.objects.create(poster=stale, title="Mine bounty", description="x" * 30, reward=50,
                              deadline=timezone.now() + timedelta(days=2))
        self.age(stale)
        response = self.signup("new@example.org", "squat", PW2)
        self.assertContains(response, "already taken")
        self.assertTrue(User.objects.filter(pk=stale.pk).exists())


class SignupRaceTests(TransactionTestCase):
    """Real threads, real commits (TransactionTestCase), so the DB-level behaviour is exercised."""

    def test_two_signups_racing_on_one_address_end_with_one_user_and_no_error(self):
        barrier = threading.Barrier(2)
        real_lookup = services._lookup_holder

        def lookup_then_rendezvous(email):
            holder = real_lookup(email)
            try:
                # Both threads have now looked the address up and found nothing. With the
                # advisory lock the second never gets here until the first commits, so the
                # barrier times out and the first proceeds; without it both insert.
                barrier.wait(timeout=1.5)
            except threading.BrokenBarrierError:
                pass
            return holder

        errors = []

        def worker(name):
            try:
                services.signup(name, "race@example.org", "Race-pass-12345")
            except BaseException as exc:  # noqa: BLE001: the test reports any failure
                errors.append(exc)
            finally:
                connection.close()  # each thread owns a connection; do not leak it

        with mock.patch.object(services, "_lookup_holder", lookup_then_rendezvous):
            threads = [threading.Thread(target=worker, args=(f"racer{i}",)) for i in range(2)]
            [t.start() for t in threads]
            [t.join(timeout=30) for t in threads]

        self.assertEqual(errors, [])
        self.assertEqual(User.objects.filter(email__iexact="race@example.org").count(), 1)

    def test_losing_the_unique_constraint_race_retries_instead_of_failing(self):
        """The advisory lock only exists on PostgreSQL; the retry is the portable backstop.
        Simulated: the first insert hits the email constraint as if another request just won."""
        real_create = User.objects.create_user
        calls = []

        def create_losing_once(*args, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise IntegrityError('duplicate key value violates unique constraint "accounts_user_email_ci_uniq"')
            return real_create(*args, **kwargs)

        with mock.patch.object(User.objects, "create_user", create_losing_once):
            services.signup("late", "late@example.org", "Race-pass-12345")
        self.assertEqual(len(calls), 2)
        self.assertEqual(User.objects.filter(email="late@example.org").count(), 1)

    def test_a_username_constraint_violation_is_reported_as_taken_not_retried(self):
        with mock.patch.object(User.objects, "create_user",
                               side_effect=IntegrityError('duplicate key value violates unique constraint "accounts_user_username_ci_uniq"')):
            with self.assertRaises(services.UsernameTaken):
                services.signup("dupe", "dupe@example.org", "Race-pass-12345")
