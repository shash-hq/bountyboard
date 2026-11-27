"""Row locks that no test depended on (found by removing each one in turn).

Each test holds the row, does the first operation inside that transaction, starts the second one on
another connection and commits only once it is waiting. Without the lock under test, the second
operation decides on the state from before the commit.
"""
import unittest
from datetime import timedelta

from django.contrib.auth.models import Permission
from django.db import connection, transaction
from django.test import TransactionTestCase, override_settings
from django.utils import timezone

from apps.bounties.tests_races import in_worker, wait_until_a_lock_request_is_waiting

from . import desk, services
from .errors import VerifierRateLimited
from .models import CampusVerification, User
from .testing import create_verified_user

PW = "pw-12345-abc"


@unittest.skipUnless(connection.vendor == "postgresql", "race tests need PostgreSQL row locks")
class LockTestCase(TransactionTestCase):
    def second_while_first_is_uncommitted(self, row_pk, first, second):
        worker, outcome = in_worker(second)
        with transaction.atomic():
            User.objects.select_for_update().get(pk=row_pk)
            first()
            worker.start()
            wait_until_a_lock_request_is_waiting()
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "worker hung")
        return outcome


@override_settings(VERIFIER_LIMIT_HOURLY=1)
class VerifierLimitRaceTests(LockTestCase):
    def test_two_simultaneous_verifications_cannot_exceed_the_verifier_limit(self):
        verifier = create_verified_user("vol", "vol@gmail.com", PW)
        verifier.user_permissions.add(Permission.objects.get(codename="verify_student"))
        first, second = (create_verified_user(n, f"{n}@gmail.com", PW) for n in ("stu1", "stu2"))
        now = timezone.now()

        def verify(subject, reg_no):
            return desk.verify_in_person(
                verifier.pk, subject.username, desk.presence_code(subject.pk, now), reg_no, "Asha Verma", now=now,
            )

        outcome = self.second_while_first_is_uncommitted(
            verifier.pk, lambda: verify(first, "11111111"), lambda: verify(second, "22222222"),
        )
        self.assertIsInstance(outcome.get("err"), VerifierRateLimited, outcome)
        self.assertEqual(CampusVerification.objects.count(), 1)


def unverified_with_link(name, token="the-token"):
    user = User.objects.create_user(name, f"{name}@gmail.com", PW)
    user.email_token_hash = services._token_hash(user, token)
    user.email_token_expires_at = timezone.now() + timedelta(hours=1)
    user.save(update_fields=["email_token_hash", "email_token_expires_at"])
    return user


class ConfirmEmailRaceTests(LockTestCase):
    def test_one_link_confirms_once_even_when_used_twice_at_once(self):
        user = unverified_with_link("twice")

        def confirm():
            return services.confirm_email(user.pk, "the-token", PW)

        outcome = self.second_while_first_is_uncommitted(user.pk, confirm, confirm)
        self.assertIsInstance(outcome.get("err"), services.InvalidToken, outcome)

    def test_a_signup_cannot_delete_an_account_that_is_being_confirmed(self):
        user = unverified_with_link("owner")
        outcome = self.second_while_first_is_uncommitted(
            user.pk,
            lambda: services.confirm_email(user.pk, "the-token", PW),
            lambda: services.signup("someone-else", user.email, PW),
        )
        self.assertNotIn("err", outcome)
        self.assertTrue(User.objects.filter(pk=user.pk, email_verified_at__isnull=False).exists())
        self.assertEqual(User.objects.count(), 1)
