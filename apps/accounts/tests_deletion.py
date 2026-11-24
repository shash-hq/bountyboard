"""Account deletion = anonymize in place. The binding and the ban history must survive it; personal
data must not; in-flight work blocks it; counterparties keep their history."""
from datetime import timedelta

from django.contrib.auth.models import Group
from django.test import TestCase, TransactionTestCase, override_settings
from django.utils import timezone

from apps.bounties.models import Bounty, BountyStatus, Claim, ClaimStatus
from apps.bounties.tests_races import run_together

from . import campus, deletion, desk, moderation
from .errors import AccountsError, DeletionBlocked, InvalidState
from .models import AuditEvent, CampusVerification, IdentityReview, StudentIdentity, User
from .services import purge_stale_unverified, signup, UsernameTaken
from .testing import create_verified_user

PW = "pw-12345-abc"
REG = "87654321"


def bounty(poster, status=BountyStatus.OPEN, title="Some task"):
    return Bounty.objects.create(
        poster=poster, title=title, description="x" * 30, reward=100,
        deadline=timezone.now() + timedelta(days=3), status=status,
    )


def claim(b, claimant, status=ClaimStatus.PENDING):
    return Claim.objects.create(bounty=b, claimant=claimant, message="m" * 12, status=status)


def accepted(b, claimant, bounty_status=BountyStatus.CLAIMED):
    """A bounty in a state where `claimant` is the accepted claimant (set directly: the fixture is
    the precondition, not the thing under test)."""
    c = claim(b, claimant, ClaimStatus.ACCEPTED)
    Bounty.objects.filter(pk=b.pk).update(status=bounty_status, accepted_claim=c)
    return c


def finished(poster, claimant, title):
    """A DONE bounty with an ACCEPTED claim. Built while OPEN (Claim.save refuses otherwise), then
    moved to its final state directly: the fixture is the precondition, not the thing under test."""
    b = bounty(poster, title=title)
    c = claim(b, claimant)
    Claim.objects.filter(pk=c.pk).update(status=ClaimStatus.ACCEPTED)
    Bounty.objects.filter(pk=b.pk).update(status=BountyStatus.DONE, accepted_claim=c)
    return b, c


class Base(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.vol = create_verified_user("vol", "vol@gmail.com", PW)
        self.vol.groups.add(Group.objects.get(name="Verifiers"))
        self.mod = create_verified_user("mod", "mod@gmail.com", PW)
        self.mod.groups.add(Group.objects.get(name="Moderators"))
        self.stu = create_verified_user(
            "stu", "stu.private@gmail.com", PW, first_name="Asha", last_name="Verma", contact_handle="+91 98765 43210"
        )
        self.other = create_verified_user("other", "other@gmail.com", PW)

    def desk_verify(self, who=None, name="Asha Verma", reg=REG):
        who = who or self.stu
        return desk.verify_in_person(self.vol.pk, who.username, desk.presence_code(who.pk, self.now), reg, name, now=self.now)


class AnonymizeTests(Base):
    def test_personal_data_is_gone_and_the_account_is_dead(self):
        self.desk_verify()
        self.stu.groups.add(Group.objects.get(name="Moderators"))
        old_hash = self.stu.password
        deletion.delete_account(self.stu.pk, now=self.now)
        u = User.objects.get(pk=self.stu.pk)
        self.assertEqual(u.deleted_at, self.now)
        self.assertFalse(u.is_active)
        self.assertTrue(u.username.startswith("deleted-") and u.email.endswith("@deleted.invalid"))
        self.assertEqual((u.first_name, u.last_name, u.contact_handle), ("", "", ""))
        self.assertFalse(u.has_usable_password())
        self.assertNotEqual(u.password, old_hash)
        self.assertFalse(u.groups.exists() or u.user_permissions.exists() or u.is_staff or u.is_superuser)
        for secret in ("stu", "private", "Asha", "Verma"):
            self.assertNotIn(secret.lower(), " ".join(str(v) for v in [u.username, u.email, u.first_name, u.last_name]).lower().replace("deleted", ""))
        self.assertFalse(campus.is_campus_verified(u, self.now))

    def test_card_name_and_campus_email_snapshot_are_wiped_but_the_rows_stay(self):
        self.desk_verify()
        em = User.objects.create_user("em", "em@lpu.co.in", PW, email_verified_at=self.now)
        campus.verify_campus_email(em, "11112222", self.now)
        for u in (self.stu, em):
            deletion.delete_account(u.pk, now=self.now)
        rows = list(CampusVerification.objects.defer(None).filter(user__in=[self.stu, em]))
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r.revoke_reason, "ACCOUNT_DELETED")
            self.assertIsNone(r.card_name_enc)
            self.assertFalse(r.verified_email)

    def test_only_the_deleted_users_pending_claims_are_withdrawn(self):
        c = claim(bounty(self.other), self.stu)
        c2 = claim(bounty(self.other, title="Other task"), create_verified_user("z", "z@gmail.com", PW))
        deletion.delete_account(self.stu.pk)
        c.refresh_from_db()
        c2.refresh_from_db()
        self.assertEqual(c.status, ClaimStatus.WITHDRAWN)
        self.assertEqual(c2.status, ClaimStatus.PENDING)  # someone else's claim is untouched

    def test_open_bounties_are_cancelled_and_pending_claims_withdrawn_through_the_services(self):
        mine = bounty(self.stu)
        theirs = bounty(self.other)
        pending = claim(theirs, self.stu)
        deletion.delete_account(self.stu.pk)
        mine.refresh_from_db(); pending.refresh_from_db()
        self.assertEqual(mine.status, BountyStatus.CANCELLED)
        self.assertEqual(pending.status, ClaimStatus.WITHDRAWN)

    def test_counterparties_keep_their_history(self):
        done, done_claim = finished(self.stu, self.other, "Finished job")
        theirs, their_claim = finished(self.other, self.stu, "Their finished job")
        deletion.delete_account(self.stu.pk)
        self.assertEqual(Bounty.objects.filter(pk__in=[done.pk, theirs.pk]).count(), 2)
        self.assertEqual(Claim.objects.get(pk=done_claim.pk).status, ClaimStatus.ACCEPTED)
        self.assertEqual(Claim.objects.get(pk=their_claim.pk).claimant_id, self.stu.pk)

    def test_exactly_one_audit_row_and_a_second_delete_is_refused(self):
        self.desk_verify()
        deletion.delete_account(self.stu.pk)
        self.assertEqual(AuditEvent.objects.filter(action="ACCOUNT_DELETED", subject=self.stu).count(), 1)
        with self.assertRaises(InvalidState):
            deletion.delete_account(self.stu.pk)
        self.assertEqual(AuditEvent.objects.filter(action="ACCOUNT_DELETED").count(), 1)

    def test_unknown_user_is_refused(self):
        with self.assertRaises(InvalidState):
            deletion.delete_account(999999)

    def test_a_logged_in_session_stops_working(self):
        self.client.force_login(self.stu)
        self.assertEqual(self.client.get("/board/").status_code, 200)
        deletion.delete_account(self.stu.pk)
        self.assertEqual(self.client.get("/board/").status_code, 302)  # to login

    def test_old_username_and_email_become_available_again(self):
        deletion.delete_account(self.stu.pk)
        signup("stu", "stu.private@gmail.com", "Zx9!long-pass")
        self.assertEqual(User.objects.filter(username="stu", deleted_at__isnull=True).count(), 1)


class InFlightWorkTests(Base):
    def test_a_claimed_bounty_as_poster_blocks_and_changes_nothing(self):
        b = bounty(self.stu)
        accepted(b, self.other)
        still_open = bounty(self.stu, title="Another task")
        with self.assertRaises(DeletionBlocked) as cm:
            deletion.delete_account(self.stu.pk)
        self.assertIn("in progress", cm.exception.messages[0])
        still_open.refresh_from_db()
        self.assertEqual(still_open.status, BountyStatus.OPEN)  # pre-check runs before any cleanup
        self.assertIsNone(User.objects.get(pk=self.stu.pk).deleted_at)
        self.assertFalse(AuditEvent.objects.filter(action="ACCOUNT_DELETED").exists())

    def test_being_the_accepted_claimant_on_a_claimed_bounty_blocks(self):
        accepted(bounty(self.other), self.stu)
        with self.assertRaises(DeletionBlocked):
            deletion.delete_account(self.stu.pk)
        self.assertTrue(User.objects.get(pk=self.stu.pk).is_active)

    def test_finishing_the_work_unblocks_it(self):
        b = bounty(self.other)
        accepted(b, self.stu)
        with self.assertRaises(DeletionBlocked):
            deletion.delete_account(self.stu.pk)
        Bounty.objects.filter(pk=b.pk).update(status=BountyStatus.DONE)
        deletion.delete_account(self.stu.pk)
        self.assertIsNotNone(User.objects.get(pk=self.stu.pk).deleted_at)


class BanStateSurvivesDeletionTests(Base):
    """The point of the whole change: deleting must not free the number or erase the ban."""

    def banned_then_deleted(self):
        self.desk_verify()
        moderation.revoke_verification(self.mod.pk, self.stu.pk, "POLICY_VIOLATION", "scam", now=self.now)
        deletion.delete_account(self.stu.pk)

    def test_binding_and_revocation_history_survive(self):
        self.desk_verify()
        digest = StudentIdentity.objects.get(user=self.stu).reg_no_hmac
        moderation.revoke_verification(self.mod.pk, self.stu.pk, "POLICY_VIOLATION", "scam", now=self.now)
        deletion.delete_account(self.stu.pk)
        self.assertEqual(StudentIdentity.objects.get(user=self.stu).reg_no_hmac, digest)
        reasons = list(CampusVerification.objects.filter(user=self.stu).values_list("revoke_reason", flat=True))
        self.assertEqual(reasons, ["POLICY_VIOLATION"])  # not overwritten by ACCOUNT_DELETED

    def test_the_same_card_on_a_new_account_still_collides_and_is_flagged(self):
        self.banned_then_deleted()
        again = create_verified_user("again", "again@gmail.com", PW)
        v = self.desk_verify(again)
        self.assertIsNone(v.identity_id)
        review = IdentityReview.objects.get(claimant=again)
        self.assertEqual(review.existing_identity.user_id, self.stu.pk)
        self.assertEqual(StudentIdentity.objects.count(), 1)

    def test_a_moderator_can_still_release_the_binding_of_a_deleted_account(self):
        self.banned_then_deleted()
        moderation.release_identity(self.mod.pk, self.stu.pk, "appeal upheld")
        self.assertFalse(StudentIdentity.objects.filter(user=self.stu).exists())

    def test_existing_purge_and_replace_paths_cannot_remove_the_deleted_user(self):
        self.banned_then_deleted()
        User.objects.filter(pk=self.stu.pk).update(date_joined=self.now - timedelta(days=30), email_verified_at=None)
        self.assertEqual(purge_stale_unverified(), 0)
        with self.assertRaises(UsernameTaken):
            signup(User.objects.get(pk=self.stu.pk).username, "x@gmail.com", "Zx9!long-pass")
        self.assertTrue(User.objects.filter(pk=self.stu.pk).exists())


class ConcurrentDeleteTests(TransactionTestCase):
    def test_two_simultaneous_deletes_of_one_account_yield_exactly_one_success(self):
        for _ in range(8):
            u = create_verified_user(f"u{User.objects.count()}", f"u{User.objects.count()}@gmail.com", PW)
            out = run_together(lambda: deletion.delete_account(u.pk), lambda: deletion.delete_account(u.pk))
            kinds = sorted(k for k, _ in out)
            self.assertEqual(kinds, ["err", "ok"], out)
            self.assertIsInstance([v for k, v in out if k == "err"][0], AccountsError)
            self.assertEqual(AuditEvent.objects.filter(action="ACCOUNT_DELETED", subject=u).count(), 1)
