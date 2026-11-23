from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from . import campus, staff
from .models import AuditEvent
from .services import confirm_email
from .tests_email_verification import PW, issue_token

User = get_user_model()


@override_settings(SITE_URL="https://bb.example.org", STAFF_EMAIL_ALLOWLIST=["prof@lpu.co.in", "ext@gmail.com"])
class AllowlistGrantTests(TestCase):
    def confirm(self, email, **user_kw):
        user = User.objects.create_user("u" + str(User.objects.count()), email, PW, **user_kw)
        uid, token = issue_token(self, user)
        confirm_email(uid, token, PW, "12345678")
        user.refresh_from_db()
        return user

    def test_allowlisted_address_gets_staff_on_confirm_and_it_is_audited(self):
        user = self.confirm("Prof@LPU.co.in")
        self.assertEqual(user.staff_source, "ALLOWLIST")
        self.assertIsNotNone(user.staff_since)
        ev = AuditEvent.objects.get(subject=user, action="STAFF_GRANTED")
        self.assertIsNone(ev.actor)

    def test_allowlist_is_by_exact_address_not_domain(self):
        for email in ["student@lpu.co.in", "prof@lpu.co.in.evil.com", "xprof@lpu.co.in", "other@gmail.com"]:
            with self.subTest(email=email):
                self.assertIsNone(self.confirm(email).staff_source)
        self.assertFalse(AuditEvent.objects.filter(action="STAFF_GRANTED").exists())

    def test_non_campus_allowlisted_address_is_staff_but_not_campus_verified(self):
        user = self.confirm("ext@gmail.com")
        self.assertEqual(user.staff_source, "ALLOWLIST")
        self.assertFalse(campus.is_campus_verified(user))

    def test_campus_verification_does_not_make_staff(self):
        user = self.confirm("student@lpu.co.in")
        self.assertTrue(campus.is_campus_verified(user))
        self.assertIsNone(user.staff_source)

    def test_signing_up_with_an_allowlisted_address_grants_nothing_until_confirmed(self):
        user = User.objects.create_user("sneaky", "prof@lpu.co.in", PW)
        user.refresh_from_db()
        self.assertIsNone(user.staff_source)

    def test_an_existing_moderator_grant_is_not_relabelled(self):
        user = User.objects.create_user("m", "prof@lpu.co.in", PW, staff_source="MODERATOR", staff_since=timezone.now())
        self.assertFalse(staff.grant_if_allowlisted(user))
        user.refresh_from_db()
        self.assertEqual(user.staff_source, "MODERATOR")

    @override_settings(STAFF_EMAIL_ALLOWLIST=[])
    def test_empty_allowlist_grants_nobody(self):
        self.assertIsNone(self.confirm("prof@lpu.co.in").staff_source)
