"""The moderator identity console: who can reach it, what it records, and what it never leaks."""
from django.urls import reverse

from .models import AuditEvent, CampusVerification, StudentIdentity
from .tests_moderation import REG, ModTestCase


class ModViewTestCase(ModTestCase):
    def setUp(self):
        super().setUp()
        self.desk_verify()
        self.student.contact_handle = "+91 99999 00000"
        self.student.save(update_fields=["contact_handle"])
        pk = self.student.pk
        self.urls = {
            "index": reverse("accounts:mod_index"),
            "user": reverse("accounts:mod_user", args=[pk]),
            "contact": reverse("accounts:mod_reveal_contact", args=[pk]),
            "card": reverse("accounts:mod_reveal_card_name", args=[pk]),
            "revoke": reverse("accounts:mod_revoke", args=[pk]),
            "release": reverse("accounts:mod_release_identity", args=[pk]),
        }


class AccessTests(ModViewTestCase):
    def test_anonymous_is_sent_to_login(self):
        for name, url in self.urls.items():
            r = self.client.post(url) if name in ("contact", "card", "revoke", "release") else self.client.get(url)
            self.assertEqual(r.status_code, 302, name)
            self.assertIn("/accounts/login/", r["Location"])

    def test_verifiers_and_students_get_403_everywhere_and_nothing_happens(self):
        for who in (self.verifier, self.student):
            self.client.force_login(who)
            for name, url in self.urls.items():
                r = self.client.post(url, {"reason": "x", "reason_code": "POLICY_VIOLATION", "note": "x", "reg_no": REG})
                self.assertEqual(r.status_code, 403, f"{who.username} POST {name}")
            self.assertEqual(self.client.get(self.urls["user"]).status_code, 403)
        self.assertFalse(AuditEvent.objects.filter(action__in=["CONTACT_VIEWED", "CARD_NAME_VIEWED", "REG_NO_LOOKUP", "REVOKED"]).exists())
        self.assertTrue(CampusVerification.objects.get(user=self.student).revoked_at is None)

    def test_state_changing_endpoints_refuse_get(self):
        self.client.force_login(self.mod)
        for name in ("contact", "card", "revoke", "release"):
            self.assertEqual(self.client.get(self.urls[name]).status_code, 405, name)


class ConsoleTests(ModViewTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.mod)

    def test_lookup_finds_the_account_and_records_it(self):
        r = self.client.post(self.urls["index"], {"reg_no": REG, "reason": "dispute 7"})
        self.assertContains(r, self.student.username)
        self.assertContains(r, REG[-4:])
        self.assertEqual(AuditEvent.objects.get(action="REG_NO_LOOKUP").note, "dispute 7")

    def test_lookup_without_a_reason_does_nothing(self):
        r = self.client.post(self.urls["index"], {"reg_no": REG, "reason": ""})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(AuditEvent.objects.filter(action="REG_NO_LOOKUP").exists())

    def test_lookup_with_a_bad_number_shows_a_field_error(self):
        r = self.client.post(self.urls["index"], {"reg_no": "abc", "reason": "x"})
        self.assertContains(r, "field-error")
        self.assertFalse(AuditEvent.objects.filter(action="REG_NO_LOOKUP").exists())

    def test_contact_is_shown_once_with_a_reason_and_is_not_cacheable(self):
        r = self.client.post(self.urls["contact"], {"reason": "safety report"})
        self.assertContains(r, "stu@gmail.com")
        self.assertContains(r, "+91 99999 00000")
        self.assertIn("no-store", r["Cache-Control"])
        self.assertEqual(AuditEvent.objects.get(action="CONTACT_VIEWED").note, "safety report")
        # The plain user page never carries it.
        self.assertNotContains(self.client.get(self.urls["user"]), "stu@gmail.com")

    def test_contact_without_a_reason_reveals_nothing_and_records_nothing(self):
        r = self.client.post(self.urls["contact"], {"reason": ""})
        self.assertNotContains(r, "stu@gmail.com")
        self.assertFalse(AuditEvent.objects.filter(action="CONTACT_VIEWED").exists())

    def test_card_name_is_shown_with_a_reason_and_recorded(self):
        r = self.client.post(self.urls["card"], {"reason": "checking a mismatch"})
        self.assertContains(r, "Asha Verma")
        self.assertIn("no-store", r["Cache-Control"])
        self.assertEqual(AuditEvent.objects.get(action="CARD_NAME_VIEWED").note, "checking a mismatch")
        self.assertNotContains(self.client.get(self.urls["user"]), "Asha Verma")

    def test_the_user_page_never_shows_the_registration_number(self):
        html = self.client.get(self.urls["user"]).content.decode()
        self.assertIn(REG[-4:], html)
        self.assertNotIn(REG, html)

    def test_revoke_goes_through_the_service_and_redirects(self):
        r = self.client.post(self.urls["revoke"], {"reason_code": "POLICY_VIOLATION", "note": "spam"})
        self.assertRedirects(r, self.urls["user"])
        v = CampusVerification.objects.get(user=self.student)
        self.assertEqual((v.revoke_reason, v.revoked_by), ("POLICY_VIOLATION", self.mod))
        self.assertEqual(AuditEvent.objects.filter(action="REVOKED").count(), 1)

    def test_system_only_reasons_cannot_be_chosen(self):
        self.client.post(self.urls["revoke"], {"reason_code": "EXPIRED", "note": ""})
        self.assertTrue(CampusVerification.objects.get(user=self.student).revoked_at is None)

    def test_release_requires_a_note_then_frees_the_binding(self):
        self.client.post(self.urls["release"], {"note": ""})
        self.assertTrue(StudentIdentity.objects.filter(user=self.student).exists())
        self.assertRedirects(self.client.post(self.urls["release"], {"note": "wrong card"}), self.urls["user"])
        self.assertFalse(StudentIdentity.objects.filter(user=self.student).exists())

    def test_unknown_user_is_a_404(self):
        self.assertEqual(self.client.get(reverse("accounts:mod_user", args=[999999])).status_code, 404)
