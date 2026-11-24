"""Campus-verification schema: every rule here is a DB constraint, so these tests insert rows
directly (ORM ``create``/``bulk_create``, no services, no forms) and expect ``IntegrityError``.

Each negative test has a positive twin in the same class, so deleting a constraint turns the
negative test red while a wrongly over-broad constraint turns the positive one red.
"""
from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone

from apps.accounts.models import (
    AuditEvent, CampusVerification, IdentityReview, StudentIdentity, User,
)
from apps.accounts.testing import create_verified_user


def mkuser(name):
    return create_verified_user(name, f"{name}@x.com", "pw-12345-abc")


def email_verification(user, **kw):
    kw.setdefault("verified_email", f"{user.username}@lpu.co.in")
    return CampusVerification.objects.create(user=user, method="CAMPUS_EMAIL", **kw)


def in_person_verification(user, verifier, **kw):
    kw.setdefault("card_name_enc", "gAAAA-ciphertext")
    return CampusVerification.objects.create(
        user=user, method="IN_PERSON", verified_by=verifier, **kw
    )


class StaffConstraintTests(TestCase):
    def test_staff_source_without_since_is_rejected(self):
        u = mkuser("a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=u.pk).update(staff_source="MODERATOR")

    def test_staff_since_without_source_is_rejected(self):
        u = mkuser("a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=u.pk).update(staff_since=timezone.now())

    def test_both_or_neither_is_accepted(self):
        u = mkuser("a")
        User.objects.filter(pk=u.pk).update(staff_source="ALLOWLIST", staff_since=timezone.now())
        User.objects.filter(pk=u.pk).update(staff_source=None, staff_since=None)


class DeletedAccountConstraintTests(TestCase):
    def test_a_deleted_account_cannot_be_active(self):
        u = mkuser("a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            User.objects.filter(pk=u.pk).update(deleted_at=timezone.now())  # is_active still True
        User.objects.filter(pk=u.pk).update(deleted_at=timezone.now(), is_active=False)

    def test_an_active_account_may_have_no_deleted_at(self):
        User.objects.filter(pk=mkuser("a").pk).update(is_active=False)  # suspended, not deleted


class StudentIdentityConstraintTests(TestCase):
    def test_reg_no_hmac_is_unique_across_users(self):
        a, b = mkuser("a"), mkuser("b")
        StudentIdentity.objects.create(
            user=a, reg_no_hmac="h" * 64, reg_no_last4="1234", source="SELF_DECLARED"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudentIdentity.objects.create(
                user=b, reg_no_hmac="h" * 64, reg_no_last4="1234", source="CARD_CHECKED"
            )
        StudentIdentity.objects.create(
            user=b, reg_no_hmac="i" * 64, reg_no_last4="9999", source="CARD_CHECKED"
        )

    def test_a_user_holds_at_most_one_binding(self):
        a = mkuser("a")
        StudentIdentity.objects.create(
            user=a, reg_no_hmac="h" * 64, reg_no_last4="1234", source="SELF_DECLARED"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            StudentIdentity.objects.create(
                user=a, reg_no_hmac="i" * 64, reg_no_last4="9999", source="SELF_DECLARED"
            )


class CampusVerificationConstraintTests(TestCase):
    def test_second_active_verification_is_rejected(self):
        a = mkuser("a")
        email_verification(a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            email_verification(a)

    def test_a_revoked_row_does_not_block_a_fresh_one(self):
        a = mkuser("a")
        email_verification(a, revoked_at=timezone.now(), revoke_reason="SUPERSEDED")
        email_verification(a)  # the renewal insert
        self.assertEqual(CampusVerification.objects.filter(user=a).count(), 2)

    def test_two_users_can_each_hold_an_active_verification(self):
        email_verification(mkuser("a"))
        email_verification(mkuser("b"))

    def test_revoked_at_without_reason_is_rejected(self):
        a = mkuser("a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            email_verification(a, revoked_at=timezone.now())

    def test_reason_without_revoked_at_is_rejected(self):
        a = mkuser("a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            email_verification(a, revoke_reason="EXPIRED")

    def test_in_person_requires_verifier(self):
        a = mkuser("a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CampusVerification.objects.create(
                user=a, method="IN_PERSON", card_name_enc="gAAAA-ciphertext"
            )

    def test_in_person_requires_card_name(self):
        a, v = mkuser("a"), mkuser("v")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CampusVerification.objects.create(user=a, method="IN_PERSON", verified_by=v)

    def test_a_revoked_in_person_row_may_have_its_card_name_wiped(self):
        """Account deletion removes the name but keeps the row as revocation history; only a
        CLOSED row may lack it (test_in_person_requires_card_name covers the active case)."""
        a, v = mkuser("a"), mkuser("v")
        row = CampusVerification.objects.create(
            user=a, method="IN_PERSON", verified_by=v, card_name_enc=None,
            revoked_at=timezone.now(), revoke_reason="ACCOUNT_DELETED",
        )
        self.assertIsNone(row.card_name_enc)

    def test_in_person_with_null_identity_is_accepted(self):
        """A reg-no collision verifies the user with identity=NULL (plan: a collision never
        blocks), so IN_PERSON must not require an identity."""
        a, v = mkuser("a"), mkuser("v")
        row = in_person_verification(a, v)
        self.assertIsNone(row.identity)

    def test_campus_email_requires_verified_email(self):
        a = mkuser("a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            CampusVerification.objects.create(user=a, method="CAMPUS_EMAIL")

    def test_campus_email_must_not_name_a_verifier(self):
        a, v = mkuser("a"), mkuser("v")
        with self.assertRaises(IntegrityError), transaction.atomic():
            email_verification(a, verified_by=v)

    def test_campus_email_must_not_link_an_identity(self):
        a = mkuser("a")
        ident = StudentIdentity.objects.create(
            user=a, reg_no_hmac="h" * 64, reg_no_last4="1234", source="SELF_DECLARED"
        )
        with self.assertRaises(IntegrityError), transaction.atomic():
            email_verification(a, identity=ident)

    def test_verifier_with_history_cannot_be_deleted(self):
        a, v = mkuser("a"), mkuser("v")
        in_person_verification(a, v)
        from django.db.models import ProtectedError
        with self.assertRaises(ProtectedError):
            v.delete()


class CardNameDeferralTests(TestCase):
    def test_default_manager_does_not_load_the_card_name(self):
        a, v = mkuser("a"), mkuser("v")
        in_person_verification(a, v, card_name_enc="gAAAA-secret")
        row = CampusVerification.objects.get(user=a)
        self.assertIn("card_name_enc", row.get_deferred_fields())
        self.assertNotIn("secret", repr(row.__dict__))
        self.assertNotIn("secret", str(row))

    def test_it_is_still_readable_when_asked_for_by_name(self):
        a, v = mkuser("a"), mkuser("v")
        in_person_verification(a, v, card_name_enc="gAAAA-secret")
        row = CampusVerification.objects.only("card_name_enc").get(user=a)
        self.assertEqual(row.card_name_enc, "gAAAA-secret")


class IdentityReviewConstraintTests(TestCase):
    def mkreview(self, claimant, **kw):
        kw.setdefault("claimed_hmac", "h" * 64)
        kw.setdefault("claimed_last4", "1234")
        kw.setdefault("claimant_source", "SELF_DECLARED")
        return IdentityReview.objects.create(claimant=claimant, **kw)

    def test_second_open_review_for_a_claimant_is_rejected(self):
        a = mkuser("a")
        self.mkreview(a)
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.mkreview(a)

    def test_a_resolved_review_does_not_block_a_new_open_one(self):
        a = mkuser("a")
        self.mkreview(
            a, status="RESOLVED", resolution="BOTH_LEGIT_TYPO", resolved_at=timezone.now()
        )
        self.mkreview(a)

    def test_resolved_without_resolution_is_rejected(self):
        a = mkuser("a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.mkreview(a, status="RESOLVED", resolved_at=timezone.now())

    def test_open_with_a_resolution_is_rejected(self):
        a = mkuser("a")
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.mkreview(a, resolution="KEEP_EXISTING")


class AuditEventRetentionTests(TestCase):
    def test_trail_survives_deleting_both_users(self):
        actor, subject = mkuser("actor"), mkuser("subject")
        ev = AuditEvent.objects.create(actor=actor, subject=subject, action="REVOKED")
        actor.delete()
        subject.delete()
        ev.refresh_from_db()
        self.assertIsNone(ev.actor_id)
        self.assertIsNone(ev.subject_id)
