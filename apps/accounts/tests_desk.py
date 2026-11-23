"""verify_in_person: permission, presence code, rate limit, identity cases, card-name storage."""
from datetime import timedelta

from django.contrib.auth.models import Group
from django.test import TestCase, override_settings
from django.utils import timezone

from . import campus, desk
from .errors import InvalidPresenceCode, NotPermitted, SubjectNotFound, VerifierRateLimited
from .identity import InvalidCardName, InvalidRegNo, decrypt_card_name, hash_reg_no
from .models import (
    AuditEvent, CampusVerification, IdentityReview, StudentIdentity, User,
)
from .testing import create_verified_user

REG = "87654321"


def mk(name, **kw):
    return create_verified_user(name, f"{name}@gmail.com", "pw-12345-abc", **kw)


class DeskTestCase(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.verifier = mk("vol")
        self.verifier.groups.add(Group.objects.get(name="Verifiers"))
        self.student = mk("stu")

    def verify(self, subject=None, *, verifier=None, reg_no=REG, name="Asha Verma", code=None, now=None, username=None):
        subject = subject or self.student
        now = now or self.now
        return desk.verify_in_person(
            (verifier or self.verifier).pk, username or subject.username,
            code if code is not None else desk.presence_code(subject.pk, now), reg_no, name, now=now,
        )


class HappyPathTests(DeskTestCase):
    def test_verifies_binds_card_checked_encrypts_name_and_audits(self):
        v = self.verify()
        self.assertTrue(campus.is_campus_verified(self.student, self.now))
        self.assertEqual((v.method, v.verified_by_id), ("IN_PERSON", self.verifier.pk))
        ident = StudentIdentity.objects.get(user=self.student)
        self.assertEqual((ident.reg_no_hmac, ident.source, ident.reg_no_last4), (hash_reg_no(REG)[0], "CARD_CHECKED", "4321"))
        self.assertEqual(v.identity_id, ident.pk)
        ev = AuditEvent.objects.get(action="VERIFIED")
        self.assertEqual((ev.actor, ev.subject, ev.verification_id), (self.verifier, self.student, v.pk))

    def test_card_name_is_ciphertext_in_the_db_and_round_trips(self):
        self.verify(name="  Asha   Verma ")
        raw = CampusVerification.objects.only("card_name_enc").get(user=self.student).card_name_enc
        self.assertNotIn("Asha", raw)
        self.assertEqual(decrypt_card_name(raw), "Asha Verma")
        self.assertNotIn(REG, " ".join(str(x) for x in CampusVerification.objects.values().get().values()))

    def test_username_lookup_is_case_insensitive(self):
        self.verify(username="  STU ")
        self.assertTrue(campus.is_campus_verified(self.student, self.now))

    def test_in_person_renewal_supersedes_the_old_row(self):
        old = self.verify()
        later = self.now + timedelta(days=1)
        self.verify(now=later)
        old = CampusVerification.objects.get(pk=old.pk)
        self.assertEqual(old.revoke_reason, "SUPERSEDED")
        self.assertEqual(CampusVerification.objects.filter(user=self.student, revoked_at__isnull=True).count(), 1)


class PermissionTests(DeskTestCase):
    def test_only_the_verify_student_permission_allows_it(self):
        for who in [self.student, mk("rando")]:
            with self.subTest(who=who.username), self.assertRaises(NotPermitted):
                self.verify(verifier=who, subject=mk("target" + who.username))
        self.assertFalse(CampusVerification.objects.exists())

    def test_moderator_group_alone_is_not_enough(self):
        mod = mk("mod")
        mod.groups.add(Group.objects.get(name="Moderators"))
        with self.assertRaises(NotPermitted):
            self.verify(verifier=mod)

    def test_inactive_verifier_is_refused(self):
        User.objects.filter(pk=self.verifier.pk).update(is_active=False)
        with self.assertRaises(NotPermitted):
            self.verify()

    def test_permission_removed_after_login_applies_immediately(self):
        self.verify()
        self.verifier.groups.clear()
        with self.assertRaises(NotPermitted):
            self.verify(subject=mk("second"))

    def test_cannot_verify_yourself(self):
        with self.assertRaises(NotPermitted):
            self.verify(subject=self.verifier)

    def test_unknown_unconfirmed_or_inactive_subjects_are_refused(self):
        with self.assertRaises(SubjectNotFound):
            self.verify(username="ghost", code="000000")
        unconfirmed = User.objects.create_user("unc", "unc@gmail.com", "pw-12345-abc")
        with self.assertRaises(SubjectNotFound):
            self.verify(subject=unconfirmed)
        User.objects.filter(pk=self.student.pk).update(is_active=False)
        with self.assertRaises(SubjectNotFound):
            self.verify()


class PresenceCodeTests(DeskTestCase):
    def test_code_is_six_digits_per_user_and_stable_within_a_window(self):
        w = self.now.replace(second=0, microsecond=0)
        w -= timedelta(seconds=int(w.timestamp()) % 600)  # start of a window
        a = desk.presence_code(self.student.pk, w)
        self.assertRegex(a, r"^\d{6}$")
        self.assertEqual(a, desk.presence_code(self.student.pk, w + timedelta(seconds=599)))
        self.assertNotEqual(a, desk.presence_code(self.verifier.pk, w))

    def test_wrong_code_and_another_users_code_are_refused(self):
        right = desk.presence_code(self.student.pk, self.now)
        wrong = "000000" if right != "000000" else "000001"
        for code in [wrong, "", desk.presence_code(self.verifier.pk, self.now)]:
            with self.subTest(code=code), self.assertRaises(InvalidPresenceCode):
                self.verify(code=code)
        self.assertFalse(CampusVerification.objects.exists())

    def test_code_expires_after_one_extra_window(self):
        code = desk.presence_code(self.student.pk, self.now)
        self.verify(code=code, now=self.now + timedelta(seconds=600))  # previous window still ok
        User.objects.all()  # (same subject again below needs a fresh row, so use another student)
        other = mk("other")
        code2 = desk.presence_code(other.pk, self.now)
        with self.assertRaises(InvalidPresenceCode):
            self.verify(subject=other, code=code2, now=self.now + timedelta(seconds=1300))


class InputValidationTests(DeskTestCase):
    def test_bad_reg_no_or_name_is_refused_before_anything_is_written(self):
        with self.assertRaises(InvalidRegNo):
            self.verify(reg_no="123")
        with self.assertRaises(InvalidCardName):
            self.verify(name="  ")
        self.assertFalse(CampusVerification.objects.exists() or StudentIdentity.objects.exists())


@override_settings(VERIFIER_LIMIT_HOURLY=3, VERIFIER_LIMIT_DAILY=5)
class RateLimitTests(DeskTestCase):
    def students(self, n):
        return [mk(f"s{i}") for i in range(n)]

    def test_the_n_plus_first_verification_in_an_hour_is_refused(self):
        s = self.students(4)
        for x in s[:3]:
            self.verify(subject=x)
        with self.assertRaises(VerifierRateLimited):
            self.verify(subject=s[3])
        self.assertFalse(campus.is_campus_verified(s[3], self.now))
        self.assertEqual(AuditEvent.objects.filter(action="VERIFIED").count(), 3)

    def test_the_window_slides_and_the_daily_cap_still_applies(self):
        s = self.students(6)
        for i, x in enumerate(s[:3]):
            self.verify(subject=x, now=self.now + timedelta(minutes=i))
        self.verify(subject=s[3], now=self.now + timedelta(hours=2))  # hourly window has moved on
        self.verify(subject=s[4], now=self.now + timedelta(hours=2, minutes=1))
        with self.assertRaises(VerifierRateLimited):  # 5 in 24h
            self.verify(subject=s[5], now=self.now + timedelta(hours=3))
        self.verify(subject=s[5], now=self.now + timedelta(hours=25))

    def test_the_limit_is_per_verifier(self):
        other = mk("vol2")
        other.groups.add(Group.objects.get(name="Verifiers"))
        s = self.students(4)
        for x in s[:3]:
            self.verify(subject=x)
        self.verify(subject=s[3], verifier=other)

    def test_refused_attempts_do_not_consume_the_budget(self):
        s = self.students(3)
        for _ in range(5):
            with self.assertRaises(InvalidPresenceCode):
                self.verify(subject=s[0], code="x")
        for x in s:
            self.verify(subject=x)


class IdentityCaseTests(DeskTestCase):
    def test_collision_still_verifies_with_null_identity_and_one_review(self):
        first = mk("first")
        self.verify(subject=first)
        v = self.verify()  # same card number for a second account
        self.assertTrue(campus.is_campus_verified(self.student, self.now))
        self.assertIsNone(v.identity_id)
        self.assertEqual(StudentIdentity.objects.get().user, first)
        r = IdentityReview.objects.get()
        self.assertEqual((r.claimant, r.claimant_source, r.status), (self.student, "CARD_CHECKED", "OPEN"))
        self.assertEqual(r.existing_identity.user, first)

    def test_card_upgrades_a_matching_self_declared_binding(self):
        campus.verify_campus_email(User.objects.create_user("c", "c@lpu.co.in", "pw-12345-abc"), REG)
        user = User.objects.get(username="c")
        User.objects.filter(pk=user.pk).update(email_verified_at=self.now)
        v = self.verify(subject=user)
        ident = StudentIdentity.objects.get(user=user)
        self.assertEqual(ident.source, "CARD_CHECKED")
        self.assertEqual(v.identity_id, ident.pk)
        self.assertFalse(IdentityReview.objects.exists())

    def test_card_disagreeing_with_a_typed_number_verifies_and_opens_a_review(self):
        user = User.objects.create_user("c", "c@lpu.co.in", "pw-12345-abc")
        campus.verify_campus_email(user, "11111111")
        User.objects.filter(pk=user.pk).update(email_verified_at=self.now)
        before = StudentIdentity.objects.get(user=user).reg_no_hmac
        v = self.verify(subject=user)  # card shows REG, user typed 11111111
        self.assertIsNone(v.identity_id)
        self.assertEqual(StudentIdentity.objects.get(user=user).reg_no_hmac, before)  # untouched
        self.assertEqual(IdentityReview.objects.get().claimed_hmac, hash_reg_no(REG)[0])
