"""Moderator reg-no lookup and contact reveal: permission, reason, audit-before-return, no plaintext."""
from unittest import mock

from django.contrib.auth.models import Group

from . import moderation
from .errors import InvalidState, NotPermitted
from .identity import InvalidRegNo
from .models import AuditEvent, StudentIdentity
from .tests_moderation import REG, ModTestCase, mk


class LookupByRegNoTests(ModTestCase):
    def setUp(self):
        super().setUp()
        self.desk_verify()  # binds REG to self.student

    def test_a_hit_returns_the_bound_account_and_is_audited(self):
        identity = moderation.lookup_by_reg_no(self.mod.pk, REG, "dispute #12")
        self.assertEqual(identity.user, self.student)
        self.assertEqual(identity.reg_no_last4, REG[-4:])
        ev = AuditEvent.objects.get(action="REG_NO_LOOKUP")
        self.assertEqual((ev.actor, ev.subject, ev.reason, ev.note), (self.mod, self.student, "MATCH", "dispute #12"))

    def test_input_is_normalised_like_the_desk_does(self):
        spaced = f" {REG[:4]}-{REG[4:]} "
        self.assertEqual(moderation.lookup_by_reg_no(self.mod.pk, spaced, "x").user, self.student)

    def test_a_miss_is_audited_too(self):
        self.assertIsNone(moderation.lookup_by_reg_no(self.mod.pk, "11112222", "probing"))
        ev = AuditEvent.objects.get(action="REG_NO_LOOKUP")
        self.assertEqual((ev.subject, ev.reason), (None, "NO_MATCH"))

    def test_the_raw_number_is_not_written_anywhere(self):
        moderation.lookup_by_reg_no(self.mod.pk, REG, f"checking {'x'}")
        for ev in AuditEvent.objects.all():
            self.assertNotIn(REG, f"{ev.note} {ev.reason}")
        self.assertFalse(StudentIdentity.objects.filter(reg_no_hmac=REG).exists())

    def test_only_moderators_and_never_verifiers_or_students(self):
        for who in (self.verifier, self.student):
            with self.assertRaises(NotPermitted):
                moderation.lookup_by_reg_no(who.pk, REG, "x")
        self.assertEqual(AuditEvent.objects.filter(action="REG_NO_LOOKUP").count(), 0)

    def test_a_reason_is_required_and_a_malformed_number_is_rejected_unaudited(self):
        with self.assertRaises(InvalidState):
            moderation.lookup_by_reg_no(self.mod.pk, REG, "  ")
        with self.assertRaises(InvalidRegNo):
            moderation.lookup_by_reg_no(self.mod.pk, "abc", "x")
        self.assertEqual(AuditEvent.objects.filter(action="REG_NO_LOOKUP").count(), 0)

    def test_if_the_audit_write_fails_nothing_is_returned(self):
        with mock.patch.object(AuditEvent.objects, "create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                moderation.lookup_by_reg_no(self.mod.pk, REG, "x")


class RevealContactTests(ModTestCase):
    def setUp(self):
        super().setUp()
        self.student.contact_handle = "+91 99999 00000"
        self.student.save(update_fields=["contact_handle"])

    def test_a_moderator_gets_email_and_handle_and_an_audit_row(self):
        email, handle = moderation.reveal_contact_for_moderation(self.mod.pk, self.student.pk, "safety report")
        self.assertEqual((email, handle), ("stu@gmail.com", "+91 99999 00000"))
        ev = AuditEvent.objects.get(action="CONTACT_VIEWED")
        self.assertEqual((ev.actor, ev.subject, ev.note), (self.mod, self.student, "safety report"))
        self.assertNotIn("stu@gmail.com", ev.note)

    def test_a_reason_is_required(self):
        with self.assertRaises(InvalidState):
            moderation.reveal_contact_for_moderation(self.mod.pk, self.student.pk, "")
        self.assertEqual(AuditEvent.objects.filter(action="CONTACT_VIEWED").count(), 0)

    def test_verifiers_students_and_even_the_subject_are_refused(self):
        for who in (self.verifier, self.student, mk("other")):
            with self.assertRaises(NotPermitted):
                moderation.reveal_contact_for_moderation(who.pk, self.student.pk, "x")
        self.assertEqual(AuditEvent.objects.filter(action="CONTACT_VIEWED").count(), 0)

    def test_permission_comes_from_the_locked_row(self):
        self.mod.groups.remove(Group.objects.get(name="Moderators"))
        with self.assertRaises(NotPermitted):
            moderation.reveal_contact_for_moderation(self.mod.pk, self.student.pk, "x")

    def test_if_the_audit_write_fails_no_contact_is_returned(self):
        with mock.patch.object(AuditEvent.objects, "create", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                moderation.reveal_contact_for_moderation(self.mod.pk, self.student.pk, "x")
