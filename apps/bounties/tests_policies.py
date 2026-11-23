"""Moderation and visibility policies (policies.py is the single source of authorization)."""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.bounties import services
from apps.bounties.models import Bounty
from apps.bounties.policies import can_moderate, can_view_claims, can_view_hidden_bounty

User = get_user_model()


class CanModerateTests(TestCase):
    def setUp(self):
        self.plain = User.objects.create(username="plain", email="plain@example.org")
        # is_staff only opens /admin/; it must grant no moderation power.
        self.staff_only = User.objects.create(username="staffonly", email="staff@example.org", is_staff=True)
        self.moderator = User.objects.create(username="mod", email="mod@example.org")
        self.moderator.groups.add(Group.objects.get(name="Moderators"))
        self.superuser = User.objects.create_superuser("root", "root@example.org", "pw-Strong-123")
        self.poster = User.objects.create(username="poster", email="poster@example.org")
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="Policy test bounty", description="x" * 30,
            reward=100, deadline=timezone.now() + timedelta(days=2), is_hidden=True,
        )

    def test_anonymous_cannot_moderate(self):
        from django.contrib.auth.models import AnonymousUser
        self.assertFalse(can_moderate(AnonymousUser()))

    def test_plain_user_cannot_moderate(self):
        self.assertFalse(can_moderate(self.plain))

    def test_is_staff_alone_cannot_moderate(self):
        self.assertFalse(can_moderate(self.staff_only))

    def test_moderators_group_member_can_moderate(self):
        self.assertTrue(can_moderate(self.moderator))

    def test_superuser_can_moderate(self):
        self.assertTrue(can_moderate(self.superuser))

    def test_inactive_moderator_cannot_moderate(self):
        self.moderator.is_active = False
        self.moderator.save()
        self.assertFalse(can_moderate(User.objects.get(pk=self.moderator.pk)))

    def test_hidden_bounty_and_claim_list_visibility(self):
        for user, allowed in [(self.poster, True), (self.moderator, True), (self.staff_only, False), (self.plain, False)]:
            with self.subTest(user=user.username):
                self.assertEqual(can_view_hidden_bounty(user, self.bounty), allowed)
                self.assertEqual(can_view_claims(user, self.bounty), allowed)

    def test_staff_only_user_gets_403_on_mod_queue_and_hide(self):
        self.client.force_login(self.staff_only)
        self.assertEqual(self.client.get(reverse("bounties:mod_queue")).status_code, 403)
        self.assertEqual(self.client.post(reverse("bounties:toggle_hide", args=[self.bounty.pk])).status_code, 403)

    def test_moderator_reaches_mod_queue(self):
        self.client.force_login(self.moderator)
        self.assertEqual(self.client.get(reverse("bounties:mod_queue")).status_code, 200)

    def test_staff_only_user_cannot_hide_via_service(self):
        from django.core.exceptions import PermissionDenied
        with self.assertRaises(PermissionDenied):
            services.hide_bounty(self.bounty.pk, self.staff_only, True)

    def test_staff_only_user_sees_no_mod_tools_on_detail(self):
        self.bounty.is_hidden = False
        self.bounty.save(update_fields=["is_hidden"])
        url = reverse("bounties:detail", kwargs={"pk": self.bounty.pk, "slug": self.bounty.slug})
        self.client.force_login(self.staff_only)
        self.assertNotContains(self.client.get(url), "MODERATOR TOOLS")
        self.client.force_login(self.moderator)
        self.assertContains(self.client.get(url), "MODERATOR TOOLS")
