"""The custom user model: identity constraints live in the DB, so tests bypass form checks."""
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class CaseInsensitiveIdentityConstraintTests(TestCase):
    """bulk_create skips forms, the manager and save(): only the DB constraint can stop these."""

    def test_email_differing_only_by_case_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([
                User(username="one", email="A@x.com"),
                User(username="two", email="a@x.com"),
            ])
        self.assertEqual(User.objects.count(), 0)

    def test_username_differing_only_by_case_is_rejected(self):
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([
                User(username="Arjun", email="one@x.com"),
                User(username="arjun", email="two@x.com"),
            ])
        self.assertEqual(User.objects.count(), 0)

    def test_constraint_also_holds_across_separate_inserts(self):
        User.objects.bulk_create([User(username="Arjun", email="A@x.com")])
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(username="arjun2", email="a@X.com")])
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.bulk_create([User(username="ARJUN", email="other@x.com")])

    def test_distinct_identities_are_accepted(self):
        User.objects.bulk_create([
            User(username="one", email="one@x.com"),
            User(username="two", email="two@x.com"),
        ])
        self.assertEqual(User.objects.count(), 2)


class UserManagerTests(TestCase):
    def test_create_user_normalizes_email(self):
        user = User.objects.create_user("u1", "  Mixed@Case.COM ", "pw-Str0ng-pass")
        self.assertEqual(user.email, "mixed@case.com")
        self.assertIsNone(user.email_verified_at)

    def test_create_superuser_is_email_verified(self):
        admin = User.objects.create_superuser("root", "root@x.com", "pw-Str0ng-pass")
        self.assertIsNotNone(admin.email_verified_at)


class GroupsMigrationTests(TestCase):
    def test_moderators_group_holds_moderate_permission(self):
        group = Group.objects.get(name="Moderators")
        self.assertEqual(
            sorted(group.permissions.values_list("content_type__app_label", "codename")),
            [("bounties", "moderate")],
        )

    def test_verifiers_group_holds_only_verify_student(self):
        group = Group.objects.get(name="Verifiers")
        self.assertEqual(
            list(group.permissions.values_list("content_type__app_label", "codename")),
            [("accounts", "verify_student")],
        )

    def test_verifier_cannot_moderate(self):
        verifier = User.objects.create_user("v", "v@x.com", "pw-Str0ng-pass")
        verifier.groups.add(Group.objects.get(name="Verifiers"))
        self.assertTrue(verifier.has_perm("accounts.verify_student"))
        self.assertFalse(verifier.has_perm("bounties.moderate"))


class MigrationDriftTests(TestCase):
    def test_models_and_migrations_agree(self):
        """Fails CI if a model changed without a migration."""
        out = StringIO()
        try:
            call_command("makemigrations", check=True, dry_run=True, stdout=out, stderr=out, verbosity=1)
        except SystemExit:
            self.fail(f"model/migration drift:\n{out.getvalue()}")


class UserAdminTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_superuser("root", "root@x.com", "pw-Str0ng-pass")
        self.client.force_login(self.admin)

    def test_email_is_read_only_on_the_change_form(self):
        target = User.objects.create_user("t", "t@x.com", "pw-Str0ng-pass")
        url = reverse("admin:accounts_user_change", args=[target.pk])
        self.assertNotContains(self.client.get(url), 'name="email"')

    def test_email_is_editable_on_the_add_form(self):
        self.assertContains(self.client.get(reverse("admin:accounts_user_add")), 'name="email"')
