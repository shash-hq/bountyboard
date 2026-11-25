"""seed_demo is development-only and must never touch real data."""
from datetime import timedelta
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.bounties.management.commands.seed_demo import SEED_USERS
from apps.bounties.models import Bounty

User = get_user_model()


def seed(**options):
    out = StringIO()
    call_command("seed_demo", stdout=out, **options)
    return out.getvalue()


def make_real_bounty(poster):
    return Bounty.objects.create(
        poster=poster, title="A real student's bounty", description="x" * 30,
        reward=100, deadline=timezone.now() + timedelta(days=2),
    )


class SeedDemoGuardTests(TestCase):
    def setUp(self):
        self.real = User.objects.create_user("realstudent", "real@gmail.com", "Str0ng-real-pw")
        self.real_bounty = make_real_bounty(self.real)

    @override_settings(DEBUG=False)
    def test_refuses_when_debug_is_off(self):
        with self.assertRaisesMessage(CommandError, "DEBUG is off"):
            seed()
        self.assertTrue(Bounty.objects.filter(pk=self.real_bounty.pk).exists())
        self.assertFalse(User.objects.filter(username="arjun_ece").exists())

    @override_settings(DEBUG=True)
    def test_runs_when_debug_is_on(self):
        seed()
        self.assertTrue(User.objects.filter(username="arjun_ece").exists())
        self.assertGreaterEqual(Bounty.objects.count(), 20)

    @override_settings(DEBUG=False)
    def test_explicit_flag_allows_a_non_debug_run(self):
        seed(allow_non_debug=True)
        self.assertTrue(User.objects.filter(username="arjun_ece").exists())


@override_settings(DEBUG=True)
class SeedDemoScopeTests(TestCase):
    def setUp(self):
        self.real = User.objects.create_user("realstudent", "real@gmail.com", "Str0ng-real-pw")
        self.real_bounty = make_real_bounty(self.real)

    def test_reseeding_deletes_only_seed_owned_bounties(self):
        seed()
        seed()  # a second run replaces seed bounties, it does not pile up or touch others
        self.assertTrue(Bounty.objects.filter(pk=self.real_bounty.pk).exists())
        seed_posters = {u[0] for u in SEED_USERS}
        other = Bounty.objects.exclude(poster__username__in=seed_posters)
        self.assertEqual(list(other), [self.real_bounty])
        self.assertEqual(Bounty.objects.filter(poster__username__in=seed_posters).count(), 25)

    def test_no_seeded_user_is_staff_or_superuser(self):
        seed()
        self.assertFalse(User.objects.filter(username__in=[u[0] for u in SEED_USERS], is_staff=True).exists())
        self.assertFalse(User.objects.filter(username__in=[u[0] for u in SEED_USERS], is_superuser=True).exists())
        mod = User.objects.get(username="campus_mod")
        self.assertTrue(mod.groups.filter(name="Moderators").exists())  # moderator by group, not staff

    def test_demo_password_is_not_public_and_is_printed_once(self):
        out = seed()
        for username, *_ in SEED_USERS:
            self.assertFalse(User.objects.get(username=username).check_password("Student123!"))
        self.assertIn("newly created demo users", out)
        pw = out.split("demo users: ")[1].split()[0]
        self.assertTrue(User.objects.get(username="priya_cs").check_password(pw))

    def test_verified_count_excludes_the_moderator(self):
        out = StringIO()
        call_command("seed_demo", stdout=out, allow_non_debug=True)
        self.assertIn(f"Verified {len(SEED_USERS) - 1} student users.", out.getvalue())

    def test_explicit_password_option(self):
        seed(password="chosen-Pass-123")
        self.assertTrue(User.objects.get(username="priya_cs").check_password("chosen-Pass-123"))

    def test_existing_seed_user_keeps_its_password(self):
        seed(password="first-Pass-123")
        seed(password="second-Pass-456")
        self.assertTrue(User.objects.get(username="priya_cs").check_password("first-Pass-123"))

    def test_refuses_to_adopt_a_real_account_with_a_seed_username(self):
        victim = User.objects.create_user("priya_cs", "priya@gmail.com", "Her-own-pw-123")
        victim_bounty = make_real_bounty(victim)
        with self.assertRaisesMessage(CommandError, "not a seed account"):
            seed()
        victim.refresh_from_db()
        self.assertTrue(victim.check_password("Her-own-pw-123"))
        self.assertTrue(Bounty.objects.filter(pk=victim_bounty.pk).exists())
        self.assertFalse(User.objects.filter(username="arjun_ece").exists())  # nothing was created

    def test_refuses_when_a_seed_username_is_staff(self):
        User.objects.create_user("campus_mod", "mod@campus.edu.in", "pw-Strong-123", is_staff=True)
        with self.assertRaisesMessage(CommandError, "not a seed account"):
            seed()


class SeedDemoLoginTests(TestCase):
    @override_settings(DEBUG=True)
    def test_seeded_accounts_can_log_in(self):
        """Login refuses unverified emails, so demo accounts must be created verified."""
        seed(password="Demo-pass-12345")
        username, email = SEED_USERS[0][0], SEED_USERS[0][1]
        self.assertIsNotNone(User.objects.get(username=username).email_verified_at)
        self.assertTrue(self.client.login(username=username, password="Demo-pass-12345"))
