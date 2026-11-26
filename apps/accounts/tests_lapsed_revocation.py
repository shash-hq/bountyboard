"""A lapsed verification can still be revoked for cause, so letting it expire doesn't dodge the block."""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import Group
from django.db import IntegrityError, transaction
from django.urls import reverse
from django.utils import timezone

from . import campus, expiry, moderation, renewal
from .errors import InvalidState
from .models import AuditEvent, CampusVerification
from .testing import campus_verify, create_verified_user
from .tests_verification_block import PW, BlockTestCase

LAPSED = timedelta(days=settings.VERIFICATION_TTL_DAYS + 1)


class LapsedRevocationTests(BlockTestCase):
    def setUp(self):
        super().setUp()
        CampusVerification.objects.filter(user=self.student).delete()
        campus_verify(self.student, verified_at=timezone.now() - LAPSED)

    def row(self):
        return CampusVerification.objects.get(user=self.student)

    def test_a_swept_lapse_can_be_revoked_for_cause_and_then_blocks(self):
        expiry.expire_lapsed()
        for reason in ("POLICY_VIOLATION", "VERIFICATION_INVALID"):
            with self.subTest(reason):
                CampusVerification.objects.filter(user=self.student).update(revoke_reason="EXPIRED")
                self.revoke(reason)
                self.assertEqual((self.row().revoke_reason, self.row().revoked_by), (reason, self.mod))
                self.assertTrue(campus.is_blocked(self.student))
                self.assertFalse(renewal.renewal_allowed(self.student))
        # The lapse stays on the audit trail beside the revocation.
        reasons = AuditEvent.objects.filter(subject=self.student, action="REVOKED").values_list("reason", flat=True)
        self.assertIn("EXPIRED", reasons)
        self.assertIn("POLICY_VIOLATION", reasons)

    def test_an_unswept_lapse_can_be_revoked_too(self):
        self.revoke()
        self.assertTrue(campus.is_blocked(self.student))

    def test_a_lapse_cannot_be_rewritten_as_a_reason_that_does_not_block(self):
        expiry.expire_lapsed()
        with self.assertRaises(InvalidState):
            self.revoke("LEFT_CAMPUS")
        self.assertEqual(self.row().revoke_reason, "EXPIRED")

    def test_only_the_latest_verification_counts(self):
        expiry.expire_lapsed()
        campus_verify(self.student)
        self.revoke("LEFT_CAMPUS")
        with self.assertRaises(InvalidState):
            self.revoke()  # the latest row ended by LEFT_CAMPUS; the older lapse is not reopened
        self.assertFalse(campus.is_blocked(self.student))

    def test_never_verified_has_nothing_to_revoke(self):
        nobody = create_verified_user("new", "new@gmail.com", PW)
        with self.assertRaises(InvalidState):
            moderation.revoke_verification(self.mod.pk, nobody.pk, "POLICY_VIOLATION")

    def test_the_moderator_page_revokes_a_lapsed_one(self):
        expiry.expire_lapsed()
        self.client.force_login(self.mod)
        self.client.post(reverse("accounts:mod_revoke", args=[self.student.pk]),
                         {"reason_code": "POLICY_VIOLATION", "note": "reported"})
        self.assertTrue(campus.is_blocked(self.student))


class BlockClearedConstraintTests(BlockTestCase):
    def test_only_a_blocking_revocation_can_be_marked_cleared(self):
        rows = CampusVerification.objects.filter(user=self.student)
        for revoked in ({}, {"revoked_at": timezone.now(), "revoke_reason": "LEFT_CAMPUS"}):
            with self.subTest(revoked), self.assertRaises(IntegrityError), transaction.atomic():
                rows.update(block_cleared_at=timezone.now(), **revoked)
