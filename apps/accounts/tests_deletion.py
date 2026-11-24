"""A revoked-as-invalid / policy-violation user must not be able to delete their way to a fresh
verification with the same card."""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone

from . import desk, moderation
from .deletion import blocks_self_deletion, ensure_self_deletion_allowed
from .errors import NotPermitted
from .models import CampusVerification, StudentIdentity, User
from .services import purge_stale_unverified, signup
from .testing import create_verified_user
from django.contrib.auth.models import Group

PW = "pw-12345-abc"


class DeletionGuardTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.mod = create_verified_user("mod", "mod@gmail.com", PW)
        self.mod.groups.add(Group.objects.get(name="Moderators"))
        vol = create_verified_user("vol", "vol@gmail.com", PW)
        vol.groups.add(Group.objects.get(name="Verifiers"))
        self.vol = vol
        self.student = create_verified_user("stu", "stu@gmail.com", PW)

    def verify(self, who=None):
        who = who or self.student
        return desk.verify_in_person(
            self.vol.pk, who.username, desk.presence_code(who.pk, self.now), "87654321", "Asha Verma", now=self.now
        )

    def test_blocked_after_invalid_or_policy_revocation_with_a_contact_support_message(self):
        for i, reason in enumerate(["VERIFICATION_INVALID", "POLICY_VIOLATION"]):
            with self.subTest(reason=reason):
                u = create_verified_user(f"s{i}", f"s{i}@gmail.com", PW)
                desk.verify_in_person(
                    self.vol.pk, u.username, desk.presence_code(u.pk, self.now), f"1000000{i}", "Asha Verma", now=self.now
                )
                moderation.revoke_verification(self.mod.pk, u.pk, reason)
                self.assertTrue(blocks_self_deletion(u))
                with self.assertRaises(NotPermitted) as cm:
                    ensure_self_deletion_allowed(u)
                self.assertIn("contact support", cm.exception.messages[0])

    def test_a_later_reverification_does_not_clear_it(self):
        self.verify()
        moderation.revoke_verification(self.mod.pk, self.student.pk, "POLICY_VIOLATION")
        self.verify()  # verified again at the desk
        with self.assertRaises(NotPermitted):
            ensure_self_deletion_allowed(self.student)

    def test_allowed_for_everyone_else(self):
        ensure_self_deletion_allowed(self.student)  # never verified
        self.verify()
        ensure_self_deletion_allowed(self.student)  # verified, active
        moderation.revoke_verification(self.mod.pk, self.student.pk, "LEFT_CAMPUS")
        ensure_self_deletion_allowed(self.student)  # left campus is not misconduct

    def test_system_closures_do_not_block(self):
        self.verify()
        CampusVerification.objects.filter(user=self.student).update(revoked_at=self.now, revoke_reason="EXPIRED")
        ensure_self_deletion_allowed(self.student)


@override_settings(SITE_URL="https://bb.example.org")
class ExistingDeletePathsCannotRemoveABannedUserTests(TestCase):
    """The only code that deletes users today purges/replaces stale unverified signups. A banned
    user who became 'unverified' again (e.g. a future email change) must survive both."""

    def setUp(self):
        now = timezone.now()
        self.mod = create_verified_user("mod", "mod@gmail.com", PW)
        self.mod.groups.add(Group.objects.get(name="Moderators"))
        vol = create_verified_user("vol", "vol@gmail.com", PW)
        vol.groups.add(Group.objects.get(name="Verifiers"))
        self.banned = create_verified_user("banned", "banned@gmail.com", PW)
        desk.verify_in_person(vol.pk, "banned", desk.presence_code(self.banned.pk, now), "87654321", "Asha Verma", now=now)
        moderation.revoke_verification(self.mod.pk, self.banned.pk, "POLICY_VIOLATION")
        # Make the account look abandoned: unverified, old, no live link.
        User.objects.filter(pk=self.banned.pk).update(email_verified_at=None, date_joined=now - timedelta(days=30))

    def test_purge_keeps_the_account_and_its_binding(self):
        self.assertEqual(purge_stale_unverified(), 0)
        self.assertTrue(User.objects.filter(pk=self.banned.pk).exists())
        self.assertTrue(StudentIdentity.objects.filter(user=self.banned).exists())

    def test_signup_with_the_same_address_does_not_replace_it(self):
        signup("someone_else", "banned@gmail.com", "Zx9!long-pass")
        self.assertTrue(User.objects.filter(pk=self.banned.pk).exists())
        self.assertTrue(StudentIdentity.objects.filter(user=self.banned).exists())

    def test_signup_with_the_same_username_is_refused_not_a_purge(self):
        from .services import UsernameTaken

        with self.assertRaises(UsernameTaken):
            signup("banned", "other@gmail.com", "Zx9!long-pass")
        self.assertTrue(User.objects.filter(pk=self.banned.pk).exists())
