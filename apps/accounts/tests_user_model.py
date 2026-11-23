"""The custom user model: identity constraints live in the DB, so tests bypass form checks."""
from io import StringIO

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from apps.accounts import desk
from apps.accounts.identity import hash_reg_no
from apps.accounts.models import AuditEvent, CampusVerification, StudentIdentity
from apps.accounts.testing import create_verified_user

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


class CampusAdminTests(TestCase):
    """The campus models are view-only in the admin, and the card name never reaches its HTML."""

    def setUp(self):
        self.root = User.objects.create_superuser("root", "root@x.com", "pw-Str0ng-pass")
        self.client.force_login(self.root)
        verifier = create_verified_user("vol", "vol@gmail.com", "pw-Str0ng-pass")
        verifier.groups.add(Group.objects.get(name="Verifiers"))
        self.student = create_verified_user("stu", "stu@gmail.com", "pw-Str0ng-pass")
        now = timezone.now()
        self.v = desk.verify_in_person(
            verifier.pk, "stu", desk.presence_code(self.student.pk, now), "87654321", "Zorblax Quimby", now=now
        )
        self.audit = AuditEvent.objects.get()
        self.ident = StudentIdentity.objects.get()

    def test_pages_render_without_the_card_name_ciphertext_or_reg_no(self):
        digest = hash_reg_no("87654321")[0]
        ciphertext = CampusVerification.objects.only("card_name_enc").get(pk=self.v.pk).card_name_enc
        pages = [
            reverse("admin:accounts_campusverification_changelist"),
            reverse("admin:accounts_campusverification_change", args=[self.v.pk]),
            reverse("admin:accounts_studentidentity_changelist"),
            reverse("admin:accounts_studentidentity_change", args=[self.ident.pk]),
            reverse("admin:accounts_auditevent_changelist"),
            reverse("admin:accounts_auditevent_change", args=[self.audit.pk]),
            reverse("admin:accounts_identityreview_changelist"),
        ]
        for url in pages:
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200)
                body = r.content.decode()
                for secret in ("Zorblax", "Quimby", "87654321", digest, ciphertext, "card_name_enc"):
                    self.assertNotIn(secret, body)

    def test_nothing_can_be_added_changed_or_deleted(self):
        for model, obj in [("campusverification", self.v), ("studentidentity", self.ident), ("auditevent", self.audit)]:
            with self.subTest(model=model):
                self.assertEqual(self.client.get(reverse(f"admin:accounts_{model}_add")).status_code, 403)
                delete = reverse(f"admin:accounts_{model}_delete", args=[obj.pk])
                self.assertEqual(self.client.post(delete, {"post": "yes"}).status_code, 403)
                change = reverse(f"admin:accounts_{model}_change", args=[obj.pk])
                self.assertEqual(self.client.post(change, {"reg_no_last4": "0000"}).status_code, 403)
        counts = (CampusVerification.objects.count(), StudentIdentity.objects.count(), AuditEvent.objects.count())
        self.assertEqual(counts, (1, 1, 1))
        self.assertEqual(StudentIdentity.objects.get().reg_no_last4, "4321")

    def test_no_bulk_delete_action_is_offered_or_honoured(self):
        url = reverse("admin:accounts_auditevent_changelist")
        self.client.post(url, {"action": "delete_selected", "_selected_action": [self.audit.pk], "post": "yes"})
        self.assertTrue(AuditEvent.objects.filter(pk=self.audit.pk).exists())
        self.assertNotContains(self.client.get(url), "delete_selected")

    def test_staff_fields_are_read_only_on_the_user_form(self):
        r = self.client.get(reverse("admin:accounts_user_change", args=[self.student.pk]))
        self.assertEqual(r.status_code, 200)
        for name in ("staff_since", "staff_source", "staff_granted_by"):
            self.assertNotContains(r, f'name="{name}"')
        self.assertContains(r, "Staff role (read-only)")
