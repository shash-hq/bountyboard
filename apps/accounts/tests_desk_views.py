"""The verifier desk page and the /me campus panel (PR 5). The service itself is in tests_desk."""
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from . import desk
from .identity import decrypt_card_name
from .models import CampusVerification, VerificationMethod
from .tests_ratelimits import GENEROUS
from .testing import create_verified_user

PW = "pw-12345-abc"
REG = "87654321"
WRONG_CODE = "xxxxxx"  # not digits, so it can never equal a real presence code
CARD_NAME = "Asha Verma Quillfeather"


def mk(name):
    return create_verified_user(name, f"{name}@gmail.com", PW)


def in_group(user, name):
    user.groups.add(Group.objects.get(name=name))
    return user


class DeskViewTestCase(TestCase):
    def setUp(self):
        cache.clear()
        self.verifier = in_group(mk("vol"), "Verifiers")
        self.student = mk("stu")
        self.client.force_login(self.verifier)

    def post(self, **overrides):
        data = {"username": "stu", "code": desk.presence_code(self.student.pk), "reg_no": REG, "card_name": CARD_NAME}
        data.update(overrides)
        return self.client.post(reverse("accounts:desk"), data)


class AccessTests(DeskViewTestCase):
    def test_anonymous_is_sent_to_login(self):
        self.client.logout()
        r = self.client.get(reverse("accounts:desk"))
        self.assertEqual(r.status_code, 302)
        self.assertIn("/login", r["Location"])

    def test_plain_user_gets_403_on_get_and_post(self):
        self.client.force_login(self.student)
        self.assertEqual(self.client.get(reverse("accounts:desk")).status_code, 403)
        self.assertEqual(self.post(username="vol").status_code, 403)

    def test_moderator_without_the_verify_permission_gets_403(self):
        self.client.force_login(in_group(mk("mod"), "Moderators"))
        self.assertEqual(self.client.get(reverse("accounts:desk")).status_code, 403)

    def test_verifier_sees_the_desk_and_a_nav_link(self):
        r = self.client.get(reverse("accounts:desk"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "VERIFY DESK")

    def test_others_do_not_get_the_nav_link(self):
        self.client.force_login(self.student)
        self.assertNotContains(self.client.get(reverse("me")), reverse("accounts:desk"))


class VerifierHasNoModerationPowerTests(DeskViewTestCase):
    """Least privilege: the Verifiers group grants the desk and nothing else."""

    def test_cannot_open_the_moderation_queue(self):
        self.assertEqual(self.client.get(reverse("bounties:mod_queue")).status_code, 403)

    def test_cannot_hide_a_bounty_or_resolve_a_report(self):
        self.assertEqual(self.client.post(reverse("bounties:toggle_hide", args=[1])).status_code, 403)
        self.assertEqual(self.client.post(reverse("bounties:resolve_report", args=[1])).status_code, 403)

    def test_no_moderator_nav_link(self):
        self.assertNotContains(self.client.get(reverse("me")), reverse("bounties:mod_queue"))

    def test_desk_page_never_shows_the_students_email(self):
        r = self.post(code=WRONG_CODE)  # fails, re-renders the form
        self.assertNotContains(r, self.student.email)


class VerifyThroughTheDeskTests(DeskViewTestCase):
    def test_success_verifies_redirects_and_echoes_no_secrets(self):
        r = self.post()
        self.assertRedirects(r, reverse("accounts:desk"), fetch_redirect_response=False)
        v = CampusVerification.objects.get(user=self.student)
        self.assertEqual((v.method, v.verified_by_id), (VerificationMethod.IN_PERSON, self.verifier.pk))
        page = self.client.get(reverse("accounts:desk"))
        self.assertContains(page, "stu is now campus-verified.")
        self.assertNotContains(page, REG)
        self.assertNotContains(page, CARD_NAME)
        self.assertEqual(decrypt_card_name(CampusVerification.objects.defer(None).get(pk=v.pk).card_name_enc), CARD_NAME)

    def test_wrong_code_verifies_nobody_and_shows_an_error(self):
        r = self.post(code=WRONG_CODE)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "wrong or has expired")
        self.assertFalse(CampusVerification.objects.exists())

    def test_unknown_user_is_reported_without_creating_anything(self):
        r = self.post(username="nobody")
        self.assertContains(r, "No such user.")
        self.assertFalse(CampusVerification.objects.exists())

    def test_bad_reg_no_and_bad_card_name_are_field_errors(self):
        self.assertContains(self.post(reg_no="abc"), "Enter a valid registration number.")
        self.assertContains(self.post(card_name="\x00"), "name")
        self.assertFalse(CampusVerification.objects.exists())

    def test_cannot_verify_yourself(self):
        r = self.client.post(reverse("accounts:desk"), {
            "username": "vol", "code": desk.presence_code(self.verifier.pk), "reg_no": REG, "card_name": CARD_NAME})
        self.assertContains(r, "cannot verify yourself")

    def test_a_failed_submission_does_not_echo_the_presence_code(self):
        code = desk.presence_code(self.student.pk)
        r = self.post(code=code, reg_no="abc")  # right code, bad reg no: the form re-renders
        self.assertContains(r, "Enter a valid registration number.")
        self.assertNotContains(r, f'value="{code}"')

    def test_get_does_not_verify(self):
        self.client.get(reverse("accounts:desk"), {"username": "stu", "code": desk.presence_code(self.student.pk)})
        self.assertFalse(CampusVerification.objects.exists())

    def test_page_is_not_cacheable(self):
        self.assertIn("no-store", self.client.get(reverse("accounts:desk"))["Cache-Control"])


class DeskRateLimitTests(DeskViewTestCase):
    @override_settings(RATE_LIMITS={**GENEROUS, "desk_per_verifier": "3/h"})
    def test_the_fourth_submission_is_refused_even_though_all_were_wrong_codes(self):
        statuses = [self.post(code=WRONG_CODE).status_code for _ in range(5)]
        self.assertEqual(statuses[:3], [200, 200, 200])
        self.assertEqual(statuses[3:], [429, 429])

    @override_settings(RATE_LIMITS={**GENEROUS, "desk_per_verifier": "2/h"})
    def test_the_budget_is_per_verifier_not_shared(self):
        other = in_group(mk("vol2"), "Verifiers")
        for _ in range(3):
            self.post(code=WRONG_CODE)
        self.client.force_login(other)
        self.assertEqual(self.post(code=WRONG_CODE).status_code, 200)
