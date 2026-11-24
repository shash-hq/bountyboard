"""Domain exceptions: shape, 403 fallback, and that users never see ``['...']``."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.bounties import services
from apps.bounties.exceptions import DomainError, InvalidTransition, NotPermitted
from apps.bounties.models import Bounty

User = get_user_model()


class ExceptionShapeTests(TestCase):
    def test_messages_and_str_are_plain_text(self):
        e = InvalidTransition("Only OPEN bounties can be cancelled.")
        self.assertEqual(e.messages, ["Only OPEN bounties can be cancelled."])
        self.assertEqual(str(e), "Only OPEN bounties can be cancelled.")

    def test_services_do_not_raise_validation_error_for_transitions(self):
        self.assertFalse(issubclass(InvalidTransition, ValidationError))
        self.assertFalse(issubclass(NotPermitted, ValidationError))
        self.assertTrue(issubclass(InvalidTransition, DomainError))

    def test_not_permitted_is_still_a_403_if_uncaught(self):
        self.assertTrue(issubclass(NotPermitted, PermissionDenied))


class ServiceErrorsReachUsersAsPlainTextTests(TestCase):
    def setUp(self):
        self.poster = User.objects.create(username="p", email="p@example.org", email_verified_at=timezone.now())
        self.other = User.objects.create(username="o", email="o@example.org", email_verified_at=timezone.now())
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="Exception text bounty", description="x" * 30,
            reward=100, deadline=timezone.now() + timedelta(days=2),
        )

    def test_invalid_transition_message_has_no_list_brackets(self):
        self.client.force_login(self.poster)
        resp = self.client.post(reverse("bounties:done", args=[self.bounty.pk]), follow=True)  # OPEN: not doable
        shown = [str(m) for m in resp.context["messages"]]
        self.assertIn("Only claimed bounties can be marked done.", shown)
        self.assertFalse(any("['" in t for t in shown), shown)

    def test_not_permitted_message_is_shown_not_a_500(self):
        self.client.force_login(self.other)
        resp = self.client.post(reverse("bounties:cancel", args=[self.bounty.pk]), follow=True)
        shown = [str(m) for m in resp.context["messages"]]
        self.assertIn("Only the bounty poster can cancel a bounty.", shown)
        self.bounty.refresh_from_db()
        self.assertEqual(self.bounty.status, "OPEN")

    def test_service_raises_not_permitted_for_non_poster(self):
        with self.assertRaises(NotPermitted):
            services.cancel_bounty(self.bounty.pk, self.other)
