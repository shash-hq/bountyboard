from datetime import timedelta
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.management import call_command
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.bounties.models import Bounty, BountyStatus, Category, Claim, ClaimStatus
from apps.bounties.templatetags.bounty_tags import inr_format, time_left

User = get_user_model()


class BountyModelAndTagTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="arjun", email="arjun@campus.edu.in", password="Password123!"
        )

    def test_inr_format_filter(self):
        """Verify inr_format correctly formats rupee amounts with Indian grouping."""
        self.assertEqual(inr_format(100), "₹100")
        self.assertEqual(inr_format(1500), "₹1,500")
        self.assertEqual(inr_format(10000), "₹10,000")
        self.assertEqual(inr_format(100000), "₹1,00,000")

    def test_time_left_filter(self):
        """Verify time_left returns accurate humanized countdowns."""
        now = timezone.now()
        past = now - timedelta(hours=1)
        self.assertEqual(time_left(past), "Expired")

        future_days = now + timedelta(days=3, hours=2)
        self.assertIn("days left", time_left(future_days))

        future_hours = now + timedelta(hours=2, minutes=30)
        self.assertIn("2h", time_left(future_hours))

    def test_bounty_clean_validations(self):
        """Verify title, description, reward and future deadline constraints."""
        future = timezone.now() + timedelta(days=2)

        # Title too short (< 8)
        b_short_title = Bounty(
            poster=self.user,
            title="Short",
            description="Valid long description exceeding twenty characters.",
            reward=100,
            deadline=future,
        )
        with self.assertRaises(ValidationError):
            b_short_title.save()

        # Description too short (< 20)
        b_short_desc = Bounty(
            poster=self.user,
            title="Valid Long Title",
            description="Too short",
            reward=100,
            deadline=future,
        )
        with self.assertRaises(ValidationError):
            b_short_desc.save()

        # Reward too low (< 10)
        b_low_reward = Bounty(
            poster=self.user,
            title="Valid Long Title",
            description="Valid long description exceeding twenty characters.",
            reward=5,
            deadline=future,
        )
        with self.assertRaises(ValidationError):
            b_low_reward.save()

        # Reward too high (> 10000)
        b_high_reward = Bounty(
            poster=self.user,
            title="Valid Long Title",
            description="Valid long description exceeding twenty characters.",
            reward=15000,
            deadline=future,
        )
        with self.assertRaises(ValidationError):
            b_high_reward.save()

        # Past deadline for new bounty
        b_past_deadline = Bounty(
            poster=self.user,
            title="Valid Long Title",
            description="Valid long description exceeding twenty characters.",
            reward=100,
            deadline=timezone.now() - timedelta(hours=2),
        )
        with self.assertRaises(ValidationError):
            b_past_deadline.save()

    def test_max_five_open_bounties_rule(self):
        """Verify user cannot have more than 5 OPEN bounties simultaneously."""
        future = timezone.now() + timedelta(days=5)

        for i in range(5):
            Bounty.objects.create(
                poster=self.user,
                title=f"Valid Test Bounty Number {i+1}",
                description="Valid long description exceeding twenty characters for testing.",
                reward=100 + i * 10,
                deadline=future,
            )

        # 6th should fail validation
        b6 = Bounty(
            poster=self.user,
            title="The Sixth Bounty Limit Test",
            description="Valid long description exceeding twenty characters for testing.",
            reward=200,
            deadline=future,
        )
        with self.assertRaises(ValidationError):
            b6.save()


class BountyViewAndFilterTests(TestCase):
    def setUp(self):
        self.user1 = User.objects.create_user(
            username="poster1", email="poster1@campus.edu.in", password="Password123!"
        )
        self.user2 = User.objects.create_user(
            username="student2", email="student2@campus.edu.in", password="Password123!"
        )
        future = timezone.now() + timedelta(days=3)

        self.b1 = Bounty.objects.create(
            poster=self.user1,
            title="Arduino Sensor Debugging Task",
            description="Fix I2C temperature sensor read errors in robotics laboratory.",
            category=Category.TECH,
            reward=300,
            deadline=future,
            status=BountyStatus.OPEN,
        )
        self.b2 = Bounty.objects.create(
            poster=self.user1,
            title="Poster Design for Cultural Fest",
            description="Create vector graphics and A3 layout for annual music festival.",
            category=Category.DESIGN,
            reward=800,
            deadline=future + timedelta(days=1),
            status=BountyStatus.OPEN,
        )

    def test_board_view_search_and_filters(self):
        """Verify ?q=, ?category=, and ?status= filtering."""
        # Search for Arduino
        res_search = self.client.get(reverse("board"), {"q": "Arduino"})
        self.assertEqual(res_search.status_code, 200)
        self.assertContains(res_search, "Arduino Sensor Debugging Task")
        self.assertNotContains(res_search, "Poster Design for Cultural Fest")

        # Category filter for Design
        res_cat = self.client.get(reverse("board"), {"category": "Design"})
        self.assertEqual(res_cat.status_code, 200)
        self.assertContains(res_cat, "Poster Design for Cultural Fest")
        self.assertNotContains(res_cat, "Arduino Sensor Debugging Task")

    def test_bounty_create_view_enforces_auth(self):
        """Verify unauthenticated user is redirected to login."""
        res = self.client.get(reverse("bounties:create"))
        self.assertEqual(res.status_code, 302)
        self.assertIn("accounts/login", res.url)

    def test_bounty_create_view_successful(self):
        """Verify authenticated user can post a bounty."""
        self.client.force_login(self.user2)
        deadline = (timezone.now() + timedelta(days=4)).strftime("%Y-%m-%dT%H:%M")
        res = self.client.post(
            reverse("bounties:create"),
            {
                "title": "Verilog ALU Simulation Help",
                "description": "Need an 8-bit arithmetic logic unit simulated with full testbench waveforms.",
                "category": "Tech",
                "reward": 450,
                "deadline": deadline,
            },
            follow=True,
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(Bounty.objects.filter(title="Verilog ALU Simulation Help").exists())

    def test_bounty_edit_permissions(self):
        """Verify only the poster can edit while OPEN."""
        # Non-poster tries to edit -> 403 Forbidden
        self.client.force_login(self.user2)
        res_forbidden = self.client.get(reverse("bounties:edit", kwargs={"pk": self.b1.pk}))
        self.assertEqual(res_forbidden.status_code, 403)

        # Poster edits -> 200 OK
        self.client.force_login(self.user1)
        res_ok = self.client.get(reverse("bounties:edit", kwargs={"pk": self.b1.pk}))
        self.assertEqual(res_ok.status_code, 200)

    def test_seed_demo_command(self):
        """Verify seed_demo runs cleanly without error."""
        call_command("seed_demo")
        self.assertGreaterEqual(Bounty.objects.count(), 20)
        self.assertTrue(User.objects.filter(username="arjun_ece").exists())
