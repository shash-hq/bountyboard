"""IdentityReview resolution and the staff role: outcomes, audit trail, races between moderators."""
import unittest

from django.db import connection
from django.test import TransactionTestCase

from apps.bounties.tests_races import in_worker, wait_until_a_lock_request_is_waiting

from . import moderation
from .errors import InvalidState, NotPermitted
from .models import (
    AuditEvent, CampusVerification, IdentityReview, ReviewResolution, StudentIdentity, User,
)
from .tests_moderation import REG, ModTestCase, mk


class ReviewTestCase(ModTestCase):
    def setUp(self):
        super().setUp()
        self.holder = self.student
        self.desk_verify(self.holder)  # binds REG to the holder
        self.newcomer = mk("newcomer")
        self.desk_verify(self.newcomer, name="Rohan Das")  # same REG: verified, unbound, review opened
        self.review = IdentityReview.objects.get(claimant=self.newcomer)

    def resolve(self, resolution, note="checked the card", review=None):
        return moderation.resolve_identity_review(self.mod.pk, (review or self.review).pk, resolution, note)

    def active(self, user):
        return CampusVerification.objects.filter(user=user, revoked_at__isnull=True).first()


class ResolveReviewTests(ReviewTestCase):
    def test_the_setup_is_a_collision_with_the_newcomer_verified_but_unbound(self):
        self.assertIsNotNone(self.active(self.newcomer))
        self.assertFalse(StudentIdentity.objects.filter(user=self.newcomer).exists())
        self.assertEqual(self.review.status, "OPEN")

    def test_keep_existing_revokes_the_newcomer_and_keeps_the_binding(self):
        self.resolve(ReviewResolution.KEEP_EXISTING)
        self.assertIsNone(self.active(self.newcomer))
        v = CampusVerification.objects.get(user=self.newcomer)
        self.assertEqual(v.revoke_reason, "VERIFICATION_INVALID")
        self.assertEqual(StudentIdentity.objects.get(reg_no_hmac=self.review.claimed_hmac).user, self.holder)
        self.assertIsNotNone(self.active(self.holder))

    def test_reassign_moves_the_binding_and_revokes_the_old_holder(self):
        self.resolve(ReviewResolution.REASSIGN)
        identity = StudentIdentity.objects.get(reg_no_hmac=self.review.claimed_hmac)
        self.assertEqual(identity.user, self.newcomer)
        self.assertEqual(self.active(self.newcomer).identity, identity)
        self.assertIsNone(self.active(self.holder))
        self.assertEqual(CampusVerification.objects.get(user=self.holder).revoke_reason, "VERIFICATION_INVALID")
        self.assertFalse(StudentIdentity.objects.filter(user=self.holder).exists())

    def test_a_typo_review_cannot_be_closed_yet_and_stays_open(self):
        with self.assertRaises(InvalidState):
            self.resolve(ReviewResolution.BOTH_LEGIT_TYPO)
        self.review.refresh_from_db()
        self.assertEqual((self.review.status, self.review.resolution), ("OPEN", None))
        self.assertFalse(AuditEvent.objects.filter(action="IDENTITY_REVIEW_RESOLVED").exists())
        self.assertEqual(StudentIdentity.objects.get(reg_no_hmac=self.review.claimed_hmac).user, self.holder)

    def test_every_outcome_records_who_decided_and_why_and_one_audit_row(self):
        self.resolve(ReviewResolution.REASSIGN, "newcomer showed the card")
        self.review.refresh_from_db()
        self.assertEqual((self.review.status, self.review.resolution, self.review.resolved_by, self.review.note),
                         ("RESOLVED", "REASSIGN", self.mod, "newcomer showed the card"))
        ev = AuditEvent.objects.get(action="IDENTITY_REVIEW_RESOLVED")
        self.assertEqual((ev.actor, ev.subject, ev.reason), (self.mod, self.newcomer, "REASSIGN"))
        # The consequences have their own rows, so the trail is complete.
        self.assertEqual(AuditEvent.objects.filter(action="IDENTITY_RELEASED", subject=self.holder).count(), 1)
        self.assertEqual(AuditEvent.objects.filter(action="REVOKED", subject=self.holder).count(), 1)

    def test_a_note_is_required_and_the_resolution_must_be_valid(self):
        with self.assertRaises(InvalidState):
            self.resolve(ReviewResolution.KEEP_EXISTING, note=" ")
        with self.assertRaises(InvalidState):
            self.resolve("DELETE_EVERYTHING")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "OPEN")

    def test_a_review_resolves_only_once(self):
        self.resolve(ReviewResolution.KEEP_EXISTING)
        with self.assertRaises(InvalidState):
            self.resolve(ReviewResolution.REASSIGN)
        self.assertEqual(StudentIdentity.objects.get(reg_no_hmac=self.review.claimed_hmac).user, self.holder)

    def test_only_moderators(self):
        for who in (self.verifier, self.student, self.newcomer):
            with self.assertRaises(NotPermitted):
                moderation.resolve_identity_review(who.pk, self.review.pk, "KEEP_EXISTING", "x")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "OPEN")

    def test_reassign_is_refused_if_the_newcomer_already_has_a_binding(self):
        StudentIdentity.objects.create(user=self.newcomer, reg_no_hmac="f" * 64, reg_no_last4="0000", source="SELF_DECLARED")
        with self.assertRaises(InvalidState):
            self.resolve(ReviewResolution.REASSIGN)
        self.assertEqual(StudentIdentity.objects.get(reg_no_hmac=self.review.claimed_hmac).user, self.holder)
        self.assertIsNotNone(self.active(self.holder))  # the whole decision rolled back

    def test_reassign_after_the_holder_was_already_released_just_rebinds(self):
        moderation.release_identity(self.mod.pk, self.holder.pk, "wrong card")
        self.resolve(ReviewResolution.REASSIGN)
        self.assertEqual(StudentIdentity.objects.get(reg_no_hmac=self.review.claimed_hmac).user, self.newcomer)

    def test_a_missing_review_is_a_domain_error(self):
        with self.assertRaises(InvalidState):
            moderation.resolve_identity_review(self.mod.pk, 999999, "KEEP_EXISTING", "x")


class StaffRoleTests(ModTestCase):
    def test_grant_then_revoke_with_audit(self):
        moderation.grant_staff(self.mod.pk, self.student.pk, "faculty, dept of CSE")
        self.student.refresh_from_db()
        self.assertEqual((self.student.staff_source, self.student.staff_granted_by), ("MODERATOR", self.mod))
        self.assertIsNotNone(self.student.staff_since)
        self.assertEqual(AuditEvent.objects.get(action="STAFF_GRANTED").note, "faculty, dept of CSE")
        moderation.revoke_staff(self.mod.pk, self.student.pk, "left the department")
        self.student.refresh_from_db()
        self.assertEqual((self.student.staff_source, self.student.staff_since, self.student.staff_granted_by), (None, None, None))
        self.assertEqual(AuditEvent.objects.filter(action="STAFF_REVOKED").count(), 1)

    def test_double_grant_and_revoke_of_a_non_staff_are_refused(self):
        with self.assertRaises(InvalidState):
            moderation.revoke_staff(self.mod.pk, self.student.pk)
        moderation.grant_staff(self.mod.pk, self.student.pk)
        with self.assertRaises(InvalidState):
            moderation.grant_staff(self.mod.pk, self.student.pk)
        self.assertEqual(AuditEvent.objects.filter(action="STAFF_GRANTED").count(), 1)

    def test_an_allowlist_grant_is_kept_and_can_be_revoked_by_a_moderator(self):
        User.objects.filter(pk=self.student.pk).update(staff_source="ALLOWLIST", staff_since=self.now)
        with self.assertRaises(InvalidState):
            moderation.grant_staff(self.mod.pk, self.student.pk)  # not relabelled MODERATOR
        moderation.revoke_staff(self.mod.pk, self.student.pk)
        self.student.refresh_from_db()
        self.assertIsNone(self.student.staff_source)

    def test_only_moderators_can_grant_or_revoke(self):
        for who in (self.verifier, self.student):
            with self.assertRaises(NotPermitted):
                moderation.grant_staff(who.pk, self.student.pk)
        self.student.refresh_from_db()
        self.assertIsNone(self.student.staff_source)


@unittest.skipUnless(connection.vendor == "postgresql", "race tests need PostgreSQL row locks")
class TwoModeratorsRaceTests(TransactionTestCase):
    """Two moderators resolving the same review serialize on the user locks; the second is refused."""

    def test_the_second_resolution_sees_the_first(self):
        from django.db import transaction

        from . import desk
        from django.utils import timezone

        now = timezone.now()
        mod1 = User.objects.create_superuser("mod1", "mod1@gmail.com", "pw-12345-abc")
        mod2 = User.objects.create_superuser("mod2", "mod2@gmail.com", "pw-12345-abc")
        # Superusers can't use the Verifiers group; they pass verify_student as superusers.
        holder = mk("holder")
        newcomer = mk("newcomer")
        desk.verify_in_person(mod1.pk, holder.username, desk.presence_code(holder.pk, now), REG, "A B", now=now)
        desk.verify_in_person(mod1.pk, newcomer.username, desk.presence_code(newcomer.pk, now), REG, "C D", now=now)
        review = IdentityReview.objects.get(claimant=newcomer)

        worker, outcome = in_worker(
            lambda: moderation.resolve_identity_review(mod2.pk, review.pk, "REASSIGN", "second")
        )
        with transaction.atomic():
            # Hold the FIRST row both moderators need in id order (the holder), as a moderator who got there
            # first would. Holding a later row would itself break the ascending order and deadlock.
            User.objects.select_for_update(no_key=True).get(pk=holder.pk)
            worker.start()
            wait_until_a_lock_request_is_waiting()
            moderation.resolve_identity_review(mod1.pk, review.pk, "KEEP_EXISTING", "first")
        worker.join(timeout=30)

        self.assertFalse(worker.is_alive(), "worker hung (deadlock?)")
        self.assertIsInstance(outcome.get("err"), InvalidState, outcome)
        review.refresh_from_db()
        self.assertEqual((review.resolution, review.resolved_by), ("KEEP_EXISTING", mod1))
        self.assertEqual(StudentIdentity.objects.get(reg_no_hmac=review.claimed_hmac).user, holder)
