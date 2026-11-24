from django.test import TestCase
from django.urls import reverse

from .models import User
from .testing import create_verified_user

PW = "pw-12345-abc"


class ContactHandleTests(TestCase):
    def setUp(self):
        self.user = create_verified_user("stu", "stu@gmail.com", PW)
        self.url = reverse("accounts:contact_handle")

    def test_requires_login(self):
        r = self.client.get(self.url)
        self.assertEqual(r.status_code, 302)
        self.assertIn("/accounts/login/", r["Location"])
        self.assertEqual(self.client.post(self.url, {"contact_handle": "x"}).status_code, 302)
        self.assertEqual(User.objects.get(pk=self.user.pk).contact_handle, "")

    def test_save_normalizes_whitespace_and_redirects_back_to_the_dashboard(self):
        self.client.force_login(self.user)
        r = self.client.post(self.url, {"contact_handle": "  +91   98765   43210 "})
        self.assertRedirects(r, reverse("me"))
        self.assertEqual(User.objects.get(pk=self.user.pk).contact_handle, "+91 98765 43210")

    def test_it_can_be_cleared(self):
        User.objects.filter(pk=self.user.pk).update(contact_handle="old")
        self.client.force_login(self.user)
        self.client.post(self.url, {"contact_handle": ""})
        self.assertEqual(User.objects.get(pk=self.user.pk).contact_handle, "")

    def test_too_long_and_control_characters_are_rejected_without_saving(self):
        self.client.force_login(self.user)
        for bad in ["x" * 101, "a\x00b", "a\x1bb"]:
            with self.subTest(bad=repr(bad)):
                r = self.client.post(self.url, {"contact_handle": bad})
                self.assertEqual(r.status_code, 200)
                self.assertTrue(r.context["form"].errors)
        self.assertEqual(User.objects.get(pk=self.user.pk).contact_handle, "")

    def test_the_form_works_without_javascript_and_the_dashboard_links_to_it(self):
        self.client.force_login(self.user)
        page = self.client.get(self.url)
        self.assertContains(page, '<form method="post"')
        self.assertContains(page, "csrfmiddlewaretoken")
        self.assertContains(self.client.get(reverse("me")), self.url)

    def test_one_user_cannot_edit_another_users_handle(self):
        other = create_verified_user("other", "other@gmail.com", PW)
        self.client.force_login(self.user)
        self.client.post(self.url, {"contact_handle": "mine", "id": other.pk, "user": other.pk})
        self.assertEqual(User.objects.get(pk=other.pk).contact_handle, "")
        self.assertEqual(User.objects.get(pk=self.user.pk).contact_handle, "mine")
