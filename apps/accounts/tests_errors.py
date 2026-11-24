"""Accounts errors share PR 2's DomainError, so views and the 403 fallback treat them uniformly."""
from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase

from apps.accounts import errors
from apps.bounties import exceptions


class AccountsErrorShapeTests(SimpleTestCase):
    def test_every_accounts_error_is_a_domain_error_with_plain_messages(self):
        for cls in (
            errors.InvalidPresenceCode, errors.VerifierRateLimited, errors.SubjectNotFound,
            errors.InvalidState, errors.DeletionBlocked,
        ):
            with self.subTest(cls=cls.__name__):
                self.assertTrue(issubclass(cls, exceptions.DomainError))
                e = cls("Plain text.")
                self.assertEqual((e.messages, str(e)), (["Plain text."], "Plain text."))

    def test_not_permitted_is_the_one_class_and_still_a_403(self):
        self.assertIs(errors.NotPermitted, exceptions.NotPermitted)
        self.assertTrue(issubclass(errors.NotPermitted, PermissionDenied))
