"""Moderator console and records: no registration number in any note, no duplicate HTML ids."""
import re
from collections import Counter

from django.test import SimpleTestCase
from django.urls import reverse

from . import moderation
from .identity import redact_reg_nos
from .models import AuditEvent, IdentityReview
from .tests_moderation import REG, ModTestCase, mk


class RedactionTests(SimpleTestCase):
    def test_numbers_are_removed_however_they_are_typed(self):
        for typed in ("12345678", "1234 5678", "12-34-56-78", "1234.5678", "reg12345678", "(12345678)"):
            with self.subTest(typed):
                out = redact_reg_nos(f"dispute about {typed} today")
                self.assertNotRegex(out, r"\d{4}")
                self.assertIn("[number removed]", out)

    def test_ordinary_notes_are_left_alone(self):
        note = "ticket #12, room 204, third visit since 2026"
        self.assertEqual(redact_reg_nos(note), note)


class NotesNeverStoreARegNoTests(ModTestCase):
    def test_lookup_and_other_moderator_notes_are_redacted(self):
        self.desk_verify()
        moderation.lookup_by_reg_no(self.mod.pk, REG, f"asked about {REG}")
        moderation.reveal_contact_for_moderation(self.mod.pk, self.student.pk, f"card {REG}")
        moderation.revoke_verification(self.mod.pk, self.student.pk, "LEFT_CAMPUS", f"was {REG}")
        self.assertTrue(AuditEvent.objects.filter(action="REG_NO_LOOKUP", note__contains="[number removed]").exists())
        self.assertFalse(AuditEvent.objects.filter(note__contains=REG).exists())


class UniqueIdTests(ModTestCase):
    def assert_unique_ids(self, url):
        self.client.force_login(self.mod)
        ids = Counter(re.findall(r'\sid="([^"]+)"', self.client.get(url).content.decode()))
        self.assertEqual({i: n for i, n in ids.items() if n > 1}, {}, url)

    def test_user_page(self):
        self.assert_unique_ids(reverse("accounts:mod_user", args=[self.student.pk]))

    def test_review_queue_with_two_reviews(self):
        self.desk_verify()
        for name in ("second", "third"):
            self.desk_verify(mk(name), name="Rohan Das")
        self.assertEqual(IdentityReview.objects.count(), 2)
        self.assert_unique_ids(reverse("accounts:mod_reviews"))

    def test_the_queue_does_not_offer_the_typo_resolution(self):
        self.desk_verify()
        self.desk_verify(mk("second"), name="Rohan Das")
        self.client.force_login(self.mod)
        response = self.client.get(reverse("accounts:mod_reviews"))
        self.assertContains(response, 'value="KEEP_EXISTING"')
        self.assertNotContains(response, 'value="BOTH_LEGIT_TYPO"')
