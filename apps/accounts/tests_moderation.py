"""Revoke, release identity, reveal card name: permission, audit, and lock behaviour."""
import threading
from datetime import timedelta

from django.contrib.auth.models import Group
from django.test import TestCase, TransactionTestCase
from django.db import transaction
from django.utils import timezone

from apps.bounties.tests_races import in_worker, wait_until_a_lock_request_is_waiting

from . import campus, desk, moderation
from .errors import InvalidState, NotPermitted
from .models import AuditEvent, CampusVerification, IdentityReview, StudentIdentity, User
from .testing import create_verified_user

REG = "87654321"


def mk(name, **kw):
    return create_verified_user(name, f"{name}@gmail.com", "pw-12345-abc", **kw)


class ModTestCase(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.mod = mk("mod")
        self.mod.groups.add(Group.objects.get(name="Moderators"))
        self.verifier = mk("vol")
        self.verifier.groups.add(Group.objects.get(name="Verifiers"))
        self.student = mk("stu")

    def desk_verify(self, subject=None, name="Asha Verma", reg=REG):
        subject = subject or self.student
        return desk.verify_in_person(
            self.verifier.pk, subject.username, desk.presence_code(subject.pk, self.now), reg, name, now=self.now
        )


class RevokeTests(ModTestCase):
    def test_revoke_closes_the_row_audits_and_keeps_the_binding(self):
        v = self.desk_verify()
        moderation.revoke_verification(self.mod.pk, self.student.pk, "VERIFICATION_INVALID", " card was borrowed ", now=self.now)
        v = CampusVerification.objects.get(pk=v.pk)
        self.assertEqual((v.revoke_reason, v.revoked_by, v.revoke_note), ("VERIFICATION_INVALID", self.mod, "card was borrowed"))
        self.assertFalse(campus.is_campus_verified(self.student, self.now))
        ev = AuditEvent.objects.get(action="REVOKED")
        self.assertEqual((ev.actor, ev.subject, ev.reason, ev.verification_id), (self.mod, self.student, "VERIFICATION_INVALID", v.pk))
        self.assertTrue(StudentIdentity.objects.filter(user=self.student).exists())

    def test_revoked_user_cannot_reuse_the_same_card_on_a_new_account(self):
        self.desk_verify()
        moderation.revoke_verification(self.mod.pk, self.student.pk, "LEFT_CAMPUS")
        other = mk("other")
        v = self.desk_verify(other)  # same card number: still collides, still flagged
        self.assertIsNone(v.identity_id)
        self.assertEqual(IdentityReview.objects.get().claimant, other)

    def test_only_moderators_may_revoke(self):
        self.desk_verify()
        for who in [self.student, self.verifier, mk("rando")]:
            with self.subTest(who=who.username), self.assertRaises(NotPermitted):
                moderation.revoke_verification(who.pk, self.student.pk, "LEFT_CAMPUS")
        self.assertTrue(campus.is_campus_verified(self.student))

    def test_system_only_and_unknown_reasons_are_refused(self):
        self.desk_verify()
        for reason in ["EXPIRED", "SUPERSEDED", "EMAIL_CHANGED", "", "whatever"]:
            with self.subTest(reason=reason), self.assertRaises(InvalidState):
                moderation.revoke_verification(self.mod.pk, self.student.pk, reason)

    def test_no_active_verification_and_double_revoke_are_refused(self):
        with self.assertRaises(InvalidState):
            moderation.revoke_verification(self.mod.pk, self.student.pk, "LEFT_CAMPUS")
        self.desk_verify()
        moderation.revoke_verification(self.mod.pk, self.student.pk, "LEFT_CAMPUS")
        with self.assertRaises(InvalidState):
            moderation.revoke_verification(self.mod.pk, self.student.pk, "LEFT_CAMPUS")
        self.assertEqual(AuditEvent.objects.filter(action="REVOKED").count(), 1)

    def test_a_lapsed_but_unswept_row_can_still_be_revoked(self):
        v = self.desk_verify()
        CampusVerification.objects.filter(pk=v.pk).update(verified_at=self.now - timedelta(days=400))
        moderation.revoke_verification(self.mod.pk, self.student.pk, "POLICY_VIOLATION")
        self.assertEqual(CampusVerification.objects.get(pk=v.pk).revoke_reason, "POLICY_VIOLATION")

    def test_overlong_note_is_refused_and_nothing_changes(self):
        self.desk_verify()
        with self.assertRaises(InvalidState):
            moderation.revoke_verification(self.mod.pk, self.student.pk, "LEFT_CAMPUS", "x" * 501)
        self.assertTrue(campus.is_campus_verified(self.student))

    def test_a_removed_moderators_membership_applies_immediately(self):
        self.desk_verify()
        self.mod.groups.clear()
        with self.assertRaises(NotPermitted):
            moderation.revoke_verification(self.mod.pk, self.student.pk, "LEFT_CAMPUS")


class ReleaseIdentityTests(ModTestCase):
    def test_release_frees_the_number_for_another_account_and_is_audited(self):
        v = self.desk_verify()
        moderation.release_identity(self.mod.pk, self.student.pk, "borrowed another student's card")
        self.assertFalse(StudentIdentity.objects.filter(user=self.student).exists())
        self.assertIsNone(CampusVerification.objects.get(pk=v.pk).identity_id)
        self.assertTrue(campus.is_campus_verified(self.student))  # release does not revoke
        self.assertEqual(AuditEvent.objects.get(action="IDENTITY_RELEASED").note, "borrowed another student's card")
        other = mk("other")
        self.assertIsNotNone(self.desk_verify(other).identity_id)  # now bindable

    def test_requires_moderator_a_note_and_an_existing_binding(self):
        self.desk_verify()
        with self.assertRaises(NotPermitted):
            moderation.release_identity(self.verifier.pk, self.student.pk, "why")
        with self.assertRaises(InvalidState):
            moderation.release_identity(self.mod.pk, self.student.pk, "  ")
        moderation.release_identity(self.mod.pk, self.student.pk, "ok")
        with self.assertRaises(InvalidState):
            moderation.release_identity(self.mod.pk, self.student.pk, "again")


class RevealCardNameTests(ModTestCase):
    def test_moderator_with_a_reason_gets_the_name_and_an_audit_row(self):
        self.desk_verify(name="Asha  Verma")
        name = moderation.reveal_card_name(self.mod.pk, self.student.pk, "dispute #12")
        self.assertEqual(name, "Asha Verma")
        ev = AuditEvent.objects.get(action="CARD_NAME_VIEWED")
        self.assertEqual((ev.actor, ev.subject, ev.note), (self.mod, self.student, "dispute #12"))
        self.assertNotIn("Asha", ev.note + ev.reason)  # the name itself is never logged

    def test_moderator_without_a_reason_is_refused_and_nothing_is_logged_or_returned(self):
        self.desk_verify()
        with self.assertRaises(InvalidState):
            moderation.reveal_card_name(self.mod.pk, self.student.pk, "")
        self.assertFalse(AuditEvent.objects.filter(action="CARD_NAME_VIEWED").exists())

    def test_owner_reads_their_own_unaudited_and_nobody_else_can(self):
        self.desk_verify()
        self.assertEqual(moderation.reveal_card_name(self.student.pk, self.student.pk), "Asha Verma")
        self.assertFalse(AuditEvent.objects.filter(action="CARD_NAME_VIEWED").exists())
        for who in [self.verifier, mk("rando")]:
            with self.subTest(who=who.username), self.assertRaises(NotPermitted):
                moderation.reveal_card_name(who.pk, self.student.pk, "curious")

    def test_no_in_person_check_means_no_name(self):
        with self.assertRaises(InvalidState):
            moderation.reveal_card_name(self.mod.pk, self.student.pk, "why")


class RevokeLocksTheSubjectRowTests(TransactionTestCase):
    """accept_claim (PR 6) will lock this same row, so holding it must block a revoke."""

    def test_revoke_waits_for_a_holder_of_the_subject_row_lock(self):
        mod = create_verified_user("admin", "admin@gmail.com", "pw-12345-abc", is_superuser=True, is_active=True)
        student = User.objects.create_user("stu", "stu@lpu.co.in", "pw-12345-abc", email_verified_at=timezone.now())
        campus.verify_campus_email(student, REG)

        with transaction.atomic():
            # FOR NO KEY UPDATE conflicts with the service's FOR UPDATE but NOT with the FOR KEY SHARE
            # that any FK insert (the audit row) takes on the user row. A plain FOR UPDATE here
            # would block the worker on that FK check even if the service took no lock at all,
            # and the test would pass with the lock removed.
            User.objects.select_for_update(no_key=True).get(pk=student.pk)
            t, out = in_worker(lambda: moderation.revoke_verification(mod.pk, student.pk, "LEFT_CAMPUS"))
            t.start()
            wait_until_a_lock_request_is_waiting()
            self.assertFalse(CampusVerification.objects.get(user=student).revoked_at)  # not revoked while we hold it
        t.join(timeout=15)
        self.assertNotIn("err", out, out)
        self.assertTrue(CampusVerification.objects.get(user=student).revoked_at)
