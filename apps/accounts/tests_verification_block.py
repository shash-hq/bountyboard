"""Revoked as invalid or for a policy violation: no way back to verified until a moderator lifts it."""
from django.contrib.auth.models import Group
from django.core import mail
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from . import campus, desk, moderation, renewal
from .errors import InvalidState, NotPermitted, VerificationBlocked
from .models import AuditEvent, CampusVerification, User
from .testing import campus_verify, create_verified_user

PW = "pw-12345-abc"
REG = "87654321"


class BlockTestCase(TestCase):
    def setUp(self):
        self.mod = create_verified_user("mod", "mod@gmail.com", PW)
        self.mod.groups.add(Group.objects.get(name="Moderators"))
        self.verifier = create_verified_user("vol", "vol@gmail.com", PW)
        self.verifier.groups.add(Group.objects.get(name="Verifiers"))
        self.student = create_verified_user("stu", "stu@lpu.co.in", PW, campus=True)

    def revoke(self, reason="POLICY_VIOLATION"):
        moderation.revoke_verification(self.mod.pk, self.student.pk, reason)

    def at_the_desk(self):
        now = timezone.now()
        return desk.verify_in_person(
            self.verifier.pk, "stu", desk.presence_code(self.student.pk, now), REG, "Asha Verma", now=now)

    def verified(self):
        return campus.is_campus_verified(self.student)


class BlockedTests(BlockTestCase):
    def test_no_path_verifies_a_user_revoked_for_cause(self):
        for reason in ("POLICY_VIOLATION", "VERIFICATION_INVALID"):
            with self.subTest(reason):
                CampusVerification.objects.filter(user=self.student).delete()
                campus_verify(self.student)
                self.revoke(reason)
                with self.assertRaises(VerificationBlocked):
                    self.at_the_desk()
                self.assertIsNone(campus.verify_campus_email(self.student, REG))
                self.assertFalse(renewal.renewal_allowed(self.student))
                with self.assertRaises(NotPermitted):
                    renewal.request_renewal(self.student.pk)
                self.assertFalse(self.verified())

    def test_a_blocked_account_cannot_renew_even_with_a_later_active_verification(self):
        self.revoke()
        campus_verify(self.student)  # a row from before this rule existed
        self.assertFalse(renewal.renewal_allowed(self.student))

    def test_leaving_campus_does_not_block(self):
        self.revoke("LEFT_CAMPUS")
        self.at_the_desk()
        self.assertTrue(self.verified())

    def test_a_later_revocation_for_another_reason_does_not_hide_an_earlier_block(self):
        self.revoke()
        campus_verify(self.student)  # a row from before this rule existed
        self.revoke("LEFT_CAMPUS")
        self.assertTrue(campus.is_blocked(self.student))

    def test_the_desk_does_not_reveal_the_block_without_a_valid_code(self):
        self.revoke()
        with self.assertRaises(desk.InvalidPresenceCode):
            desk.verify_in_person(self.verifier.pk, "stu", "000000", REG, "Asha Verma")

    def test_the_dashboard_offers_no_code_and_says_who_can_help(self):
        self.revoke()
        self.client.force_login(self.student)
        response = self.client.get(reverse("me"))
        self.assertIsNone(response.context["presence_code"])
        self.assertContains(response, "a moderator has to allow it")


class LiftTests(BlockTestCase):
    def setUp(self):
        super().setUp()
        self.revoke()

    def lift(self, actor=None, note="appeal upheld"):
        moderation.clear_verification_block((actor or self.mod).pk, self.student.pk, note)

    def test_after_lifting_the_user_can_verify_again_and_it_is_recorded(self):
        self.lift()
        self.assertFalse(self.verified())  # lifting does not itself verify anyone
        self.at_the_desk()
        self.assertTrue(self.verified())
        event = AuditEvent.objects.get(action="BLOCK_CLEARED")
        self.assertEqual((event.actor, event.subject, event.note), (self.mod, self.student, "appeal upheld"))

    def test_after_lifting_the_user_can_also_renew_by_campus_email(self):
        self.lift()
        self.assertTrue(renewal.renewal_allowed(self.student))
        with self.captureOnCommitCallbacks(execute=True):
            renewal.request_renewal(self.student.pk)
        token = mail.outbox[-1].body.split("/accounts/renew/")[1].split("/")[1]
        renewal.confirm_renewal(self.student.pk, token, REG)
        self.assertTrue(self.verified())

    def test_a_note_is_required(self):
        with self.assertRaises(InvalidState):
            self.lift(note=" ")

    def test_only_a_moderator_and_never_on_yourself(self):
        for actor in (self.verifier, self.student):
            with self.assertRaises(NotPermitted):
                self.lift(actor)
        self.student.groups.add(Group.objects.get(name="Moderators"))
        with self.assertRaises(NotPermitted):
            self.lift(User.objects.get(pk=self.student.pk))
        self.assertTrue(campus.is_blocked(self.student))

    def test_there_is_nothing_to_lift_twice(self):
        self.lift()
        with self.assertRaises(InvalidState):
            self.lift()

    def test_the_moderator_page_offers_it_and_it_works(self):
        self.client.force_login(self.mod)
        url = reverse("accounts:mod_clear_block", args=[self.student.pk])
        self.assertContains(self.client.get(reverse("accounts:mod_user", args=[self.student.pk])), url)
        self.assertEqual(self.client.get(url).status_code, 405)
        self.client.post(url, {"note": "appeal upheld"})
        self.assertFalse(campus.is_blocked(self.student))

    def test_a_non_moderator_cannot_post_it(self):
        self.client.force_login(self.verifier)
        response = self.client.post(reverse("accounts:mod_clear_block", args=[self.student.pk]), {"note": "x"})
        self.assertEqual(response.status_code, 403)
        self.assertTrue(campus.is_blocked(self.student))
