"""I4: the live OPEN-bounty cap is 1 (email-verified) or 5 (campus-verified), hidden bounties count,
and a revoked/lapsed user drops to 1 without losing what they already have."""
from datetime import timedelta

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts import moderation
from apps.accounts.models import CampusVerification
from apps.accounts.testing import campus_verify, create_verified_user
from apps.bounties import services
from apps.bounties.exceptions import LimitReached
from apps.bounties.models import Bounty, BountyStatus

PW = "pw-12345-abc"


def fields(i=0):
    return dict(
        title=f"Open cap bounty {i}", description="d" * 30, reward=100,
        deadline=timezone.now() + timedelta(days=3),
    )


class CapTests(TestCase):
    def setUp(self):
        self.email_only = create_verified_user("emailonly", "emailonly@gmail.com", PW)
        self.student = campus_verify(create_verified_user("student", "student@gmail.com", PW))

    def test_email_verified_user_may_post_exactly_one(self):
        services.create_bounty(self.email_only, **fields(1))
        with self.assertRaises(LimitReached) as cm:
            services.create_bounty(self.email_only, **fields(2))
        msg = cm.exception.messages[0]
        self.assertIn("1 open bounty", msg)
        self.assertIn("Campus-verified students can have up to 5", msg)  # tells them how to get more
        self.assertEqual(Bounty.objects.filter(poster=self.email_only).count(), 1)

    def test_campus_verified_user_may_post_exactly_five(self):
        for i in range(5):
            services.create_bounty(self.student, **fields(i))
        with self.assertRaises(LimitReached) as cm:
            services.create_bounty(self.student, **fields(6))
        self.assertIn("5 open bounties", cm.exception.messages[0])
        self.assertNotIn("Campus-verified students can have", cm.exception.messages[0])

    def test_hidden_bounties_count_toward_the_cap(self):
        """Hiding a listing must not be a way to post past the cap (I4)."""
        for i in range(5):
            services.create_bounty(self.student, **fields(i))
        Bounty.objects.filter(poster=self.student).update(is_hidden=True)
        with self.assertRaises(LimitReached):
            services.create_bounty(self.student, **fields(9))

    def test_finished_bounties_do_not_count(self):
        b = services.create_bounty(self.email_only, **fields(1))
        services.cancel_bounty(b.pk, self.email_only)
        services.create_bounty(self.email_only, **fields(2))  # the slot is free again

    def test_revoking_verification_lowers_the_cap_but_keeps_existing_bounties(self):
        for i in range(3):
            services.create_bounty(self.student, **fields(i))
        from django.contrib.auth.models import Group

        mod = create_verified_user("mod", "mod@gmail.com", PW)
        mod.groups.add(Group.objects.get(name="Moderators"))
        moderation.revoke_verification(mod.pk, self.student.pk, "LEFT_CAMPUS")
        self.assertEqual(Bounty.objects.filter(poster=self.student, status=BountyStatus.OPEN).count(), 3)
        with self.assertRaises(LimitReached):  # 3 open >= cap 1: no new posts, nothing taken away
            services.create_bounty(self.student, **fields(4))

    def test_a_lapsed_verification_lowers_the_cap_without_a_sweeper(self):
        CampusVerification.objects.filter(user=self.student).update(verified_at=timezone.now() - timedelta(days=400))
        services.create_bounty(self.student, **fields(1))
        with self.assertRaises(LimitReached):
            services.create_bounty(self.student, **fields(2))

    def test_model_validation_no_longer_enforces_the_cap(self):
        """The cap lives in the service, under the user lock. A bare Bounty.save() is not capped, so
        nothing else may create bounties for users: tests and fixtures excepted."""
        for i in range(2):
            Bounty.objects.create(poster=self.email_only, **fields(i))
        self.assertEqual(Bounty.objects.filter(poster=self.email_only).count(), 2)


class CreateViewTests(TestCase):
    def setUp(self):
        self.user = create_verified_user("viewer", "viewer@gmail.com", PW)
        self.client.force_login(self.user)

    def test_user_at_the_cap_is_redirected_with_an_explanation_before_seeing_the_form(self):
        services.create_bounty(self.user, **fields(1))
        r = self.client.get(reverse("bounties:create"), follow=True)
        self.assertRedirects(r, reverse("me"))
        self.assertContains(r, "You already have 1 open bounty")

    def test_post_past_the_cap_shows_the_message_not_a_500(self):
        services.create_bounty(self.user, **fields(1))
        r = self.client.post(reverse("bounties:create"), {"title": "Second bounty here", "description": "d" * 30,
                                                          "reward": "100", "deadline": "2099-01-01T10:00"})
        self.assertIn(r.status_code, (200, 302))
        self.assertEqual(Bounty.objects.filter(poster=self.user).count(), 1)
