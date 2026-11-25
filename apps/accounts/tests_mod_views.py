"""The moderator identity console: who can reach it, what it records, and what it never leaks."""
from django.urls import reverse

from .models import AuditEvent, CampusVerification, IdentityReview, StudentIdentity
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


class ReviewAndStaffViewTests(ModViewTestCase):
    def setUp(self):
        super().setUp()
        self.newcomer = mk_user("newcomer")
        self.desk_verify(self.newcomer, name="Rohan Das")  # same REG as the student: opens a review
        self.review = IdentityReview.objects.get(claimant=self.newcomer)
        self.resolve_url = reverse("accounts:mod_resolve_review", args=[self.review.pk])
        self.pk = self.student.pk

    def test_non_moderators_are_refused_everywhere_here(self):
        targets = [
            ("get", reverse("accounts:mod_reviews")),
            ("post", self.resolve_url),
            ("post", reverse("accounts:mod_grant_staff", args=[self.pk])),
            ("post", reverse("accounts:mod_revoke_staff", args=[self.pk])),
        ]
        for who in (self.verifier, self.student):
            self.client.force_login(who)
            for method, url in targets:
                r = getattr(self.client, method)(url, {"resolution": "KEEP_EXISTING", "note": "x"})
                self.assertEqual(r.status_code, 403, f"{who.username} {url}")
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "OPEN")
        self.student.refresh_from_db()
        self.assertIsNone(self.student.staff_source)

    def test_the_queue_lists_open_reviews_with_the_holder_and_never_the_number(self):
        self.client.force_login(self.mod)
        html = self.client.get(reverse("accounts:mod_reviews")).content.decode()
        self.assertIn("newcomer", html)
        self.assertIn(self.student.username, html)
        self.assertIn(REG[-4:], html)
        self.assertNotIn(REG, html)

    def test_resolving_runs_the_service_and_empties_the_queue(self):
        self.client.force_login(self.mod)
        r = self.client.post(self.resolve_url, {"resolution": "BOTH_LEGIT_TYPO", "note": "typo"})
        self.assertRedirects(r, reverse("accounts:mod_reviews"))
        self.review.refresh_from_db()
        self.assertEqual((self.review.status, self.review.resolution), ("RESOLVED", "BOTH_LEGIT_TYPO"))
        self.assertContains(self.client.get(reverse("accounts:mod_reviews")), "No open reviews")

    def test_resolving_without_a_note_or_with_a_bad_choice_changes_nothing(self):
        self.client.force_login(self.mod)
        self.client.post(self.resolve_url, {"resolution": "KEEP_EXISTING", "note": ""})
        self.client.post(self.resolve_url, {"resolution": "NUKE", "note": "x"})
        self.review.refresh_from_db()
        self.assertEqual(self.review.status, "OPEN")

    def test_resolving_twice_shows_a_message_instead_of_crashing(self):
        self.client.force_login(self.mod)
        data = {"resolution": "KEEP_EXISTING", "note": "x"}
        self.client.post(self.resolve_url, data)
        r = self.client.post(self.resolve_url, data, follow=True)
        self.assertContains(r, "already resolved")

    def test_grant_and_revoke_staff_from_the_user_page(self):
        self.client.force_login(self.mod)
        user_url = reverse("accounts:mod_user", args=[self.pk])
        self.assertRedirects(self.client.post(reverse("accounts:mod_grant_staff", args=[self.pk]), {"note": "CSE faculty"}), user_url)
        self.student.refresh_from_db()
        self.assertEqual(self.student.staff_source, "MODERATOR")
        self.assertContains(self.client.get(user_url), "REMOVE STAFF ROLE")
        self.client.post(reverse("accounts:mod_revoke_staff", args=[self.pk]), {"note": ""})
        self.student.refresh_from_db()
        self.assertIsNone(self.student.staff_source)


def mk_user(name):
    from .testing import create_verified_user

    return create_verified_user(name, f"{name}@gmail.com", "pw-12345-abc")
