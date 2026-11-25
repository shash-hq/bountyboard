"""Contact comes from the profile: nothing is typed at pitch time, and the accept email uses it."""
from datetime import timedelta

from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from apps.accounts.testing import create_verified_user
from apps.bounties import services
from apps.bounties.forms import ClaimForm
from apps.bounties.models import Bounty, Claim

PW = "pw-12345-abc"


class PitchHasNoContactFieldTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user("poster", "poster@gmail.com", PW, campus=True)
        self.claimant = create_verified_user("claimant", "claimant@gmail.com", PW)
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="No contact at pitch", description="d" * 30, reward=100,
            deadline=timezone.now() + timedelta(days=3),
        )

    def test_the_model_and_form_have_no_contact_field(self):
        self.assertNotIn("contact", [f.name for f in Claim._meta.get_fields()])
        self.assertEqual(list(ClaimForm().fields), ["message"])

    def test_the_page_does_not_ask_for_contact_and_a_pitch_needs_only_a_message(self):
        self.client.force_login(self.claimant)
        page = self.client.get(self.bounty.get_absolute_url())
        self.assertNotContains(page, 'name="contact"')
        r = self.client.post(reverse("bounties:claim", args=[self.bounty.pk]), {"message": "I can do this well."}, follow=True)
        self.assertContains(r, "was submitted")
        self.assertEqual(Claim.objects.filter(bounty=self.bounty, claimant=self.claimant).count(), 1)

    def test_a_stray_contact_post_field_is_ignored_not_stored_or_shown(self):
        self.client.force_login(self.claimant)
        r = self.client.post(
            reverse("bounties:claim", args=[self.bounty.pk]),
            {"message": "I can do this well.", "contact": "SNEAKY-HANDLE-777"}, follow=True,
        )
        self.assertNotContains(r, "SNEAKY-HANDLE-777")


@override_settings(SITE_URL="https://bb.example.org")
class AcceptEmailUsesTheProfileTests(TestCase):
    def setUp(self):
        self.poster = create_verified_user("poster", "poster.mail@gmail.com", PW, campus=True, contact_handle="WA-POSTER-5555")
        self.claimant = create_verified_user("claimant", "claimant.mail@gmail.com", PW, campus=True, contact_handle="WA-CLAIMANT-7777")
        self.bounty = Bounty.objects.create(
            poster=self.poster, title="Accept email bounty", description="d" * 30, reward=100,
            deadline=timezone.now() + timedelta(days=3),
        )
        self.claim = Claim.objects.create(bounty=self.bounty, claimant=self.claimant, message="m" * 12)

    def accept(self):
        with self.captureOnCommitCallbacks(execute=True):
            services.accept_claim(self.claim.pk, self.poster)
        return [m for m in mail.outbox if "accepted" in m.subject][-1]

    def test_the_claimant_is_sent_the_posters_email_and_handle_only(self):
        msg = self.accept()
        self.assertEqual(msg.to, ["claimant.mail@gmail.com"])
        self.assertIn("poster.mail@gmail.com", msg.body)
        self.assertIn("WA-POSTER-5555", msg.body)
        self.assertNotIn("WA-CLAIMANT-7777", msg.body)  # their own handle is not echoed back
        self.assertNotIn("Your Provided Handle", msg.body)

    def test_a_poster_without_a_handle_shares_just_their_email(self):
        type(self.poster).objects.filter(pk=self.poster.pk).update(contact_handle="")
        self.poster.refresh_from_db()
        body = self.accept().body
        self.assertIn("poster.mail@gmail.com", body)
        self.assertNotIn("Poster Contact Handle", body)

    def test_the_pitch_notice_to_the_poster_carries_no_claimant_contact_and_sends_no_email(self):
        from apps.bounties.models import Notification

        other = create_verified_user("pitcher", "pitcher.mail@gmail.com", PW, contact_handle="WA-PITCH-1")
        mail.outbox.clear()
        with self.captureOnCommitCallbacks(execute=True):
            services.create_claim(self.bounty.pk, other, "I can do this by Friday.")
        note = Notification.objects.get(recipient=self.poster, kind="CLAIM_RECEIVED")
        self.assertNotIn("pitcher.mail@gmail.com", note.message)
        self.assertNotIn("WA-PITCH-1", note.message)
        self.assertEqual(mail.outbox, [])  # email is reserved for acceptance
