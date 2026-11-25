from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

from apps.accounts.testing import create_verified_user

from . import inbox
from .models import Notification, NotificationKind

PW = "pw-12345-abc"


class NotifyTests(TestCase):
    def setUp(self):
        self.alice = create_verified_user("alice", "alice@gmail.com", PW)
        self.bob = create_verified_user("bob", "bob@gmail.com", PW)

    def _notify(self, user, key="k1", message="hello"):
        with transaction.atomic():
            return inbox.notify(user, NotificationKind.COUNTERPARTY_REVOKED, message, key)

    def test_same_key_is_idempotent(self):
        _, first = self._notify(self.alice)
        _, second = self._notify(self.alice, message="different text, same event")
        self.assertTrue(first)
        self.assertFalse(second)
        self.assertEqual(Notification.objects.count(), 1)

    def test_the_unique_key_is_enforced_by_the_database(self):
        make = lambda: Notification.objects.create(  # noqa: E731
            recipient=self.alice, kind=NotificationKind.COUNTERPARTY_REVOKED, message="m", idempotency_key="dup"
        )
        make()
        with self.assertRaises(IntegrityError), transaction.atomic():
            make()

    def test_refuses_to_run_outside_a_transaction(self):
        # TestCase wraps tests in a transaction, so step out of it with a plain connection check.
        from unittest import mock

        fake = mock.Mock(in_atomic_block=False)
        with mock.patch("apps.bounties.inbox.transaction.get_connection", return_value=fake):
            with self.assertRaises(RuntimeError):
                inbox.notify(self.alice, NotificationKind.COUNTERPARTY_REVOKED, "m", "k")
        self.assertEqual(Notification.objects.count(), 0)

    def test_rolls_back_with_the_caller_transaction(self):
        with self.assertRaises(ZeroDivisionError), transaction.atomic():
            inbox.notify(self.alice, NotificationKind.COUNTERPARTY_REVOKED, "m", "k")
            1 / 0
        self.assertEqual(Notification.objects.count(), 0)

    def test_recent_and_unread_are_scoped_to_the_recipient(self):
        self._notify(self.alice, "a1", "for alice")
        self._notify(self.bob, "b1", "for bob")
        self.assertEqual([n.message for n in inbox.recent_for(self.alice)], ["for alice"])
        self.assertEqual(inbox.unread_count(self.bob), 1)

    def test_mark_all_read_only_touches_the_users_own_rows(self):
        self._notify(self.alice, "a1")
        self._notify(self.bob, "b1")
        self.assertEqual(inbox.mark_all_read(self.alice), 1)
        self.assertEqual(inbox.unread_count(self.alice), 0)
        self.assertEqual(inbox.unread_count(self.bob), 1)


class MeNotificationsTests(TestCase):
    def setUp(self):
        self.alice = create_verified_user("alice", "alice@gmail.com", PW)
        self.bob = create_verified_user("bob", "bob@gmail.com", PW)
        with transaction.atomic():
            inbox.notify(self.alice, NotificationKind.COUNTERPARTY_REVOKED, "Alice <b>note</b>", "a1")
            inbox.notify(self.bob, NotificationKind.COUNTERPARTY_REVOKED, "Bob private note", "b1")

    def test_me_shows_only_own_notifications_escaped(self):
        self.client.force_login(self.alice)
        html = self.client.get(reverse("me")).content.decode()
        self.assertIn("Alice &lt;b&gt;note&lt;/b&gt;", html)
        self.assertNotIn("Bob private note", html)

    def test_mark_read_is_post_only_and_prg(self):
        url = reverse("accounts:notifications_read")
        self.assertEqual(self.client.post(url).status_code, 302)  # anonymous -> login
        self.assertEqual(inbox.unread_count(self.alice), 1)
        self.client.force_login(self.alice)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.assertRedirects(self.client.post(url), reverse("me"))
        self.assertEqual(inbox.unread_count(self.alice), 0)
        self.assertEqual(inbox.unread_count(self.bob), 1)
