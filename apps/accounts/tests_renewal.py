"""Renewing a campus verification: who may, the single-use link, and what it changes."""
import re
from datetime import timedelta

from django.conf import settings
from django.core import mail
from django.core.cache import cache
from django.db import transaction
from django.test import TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone

from apps.bounties.exceptions import NotPermitted
from apps.bounties.policies import is_campus_verified
from apps.bounties.tests_races import in_worker, wait_until_a_lock_request_is_waiting

from . import renewal
from .models import AuditEvent, CampusVerification, StudentIdentity, User
from .services import InvalidToken
from .testing import campus_verify, create_verified_user

PW = "pw-12345-abc"
TTL = timedelta(days=settings.VERIFICATION_TTL_DAYS)
LINK = re.compile(r"/accounts/renew/(\d+)/([\w-]+)/")


def campus_user(name, age=timedelta(days=10), **kw):
    u = create_verified_user(name, f"{name}@lpu.co.in", PW)
    StudentIdentity.objects.create(user=u, reg_no_hmac=(name * 64)[:64], reg_no_last4="1234", source="SELF_DECLARED")
    campus_verify(u, verified_at=timezone.now() - age, verified_email=u.email, **kw)
    return u


def link_for(user):
    with TestCase.captureOnCommitCallbacks(execute=True):  # the email goes out on commit
        renewal.request_renewal(user.pk)
    return LINK.search(mail.outbox[-1].body).groups()


class WhoMayRenewTests(TestCase):
    def test_matrix(self):
        self.assertTrue(renewal.renewal_allowed(campus_user("active")))
        self.assertTrue(renewal.renewal_allowed(campus_user("lapsed", age=TTL + timedelta(days=3))))  # not yet swept
        swept = campus_user("swept", age=TTL + timedelta(days=3))
        CampusVerification.objects.filter(user=swept).update(revoked_at=timezone.now(), revoke_reason="EXPIRED")
        self.assertTrue(renewal.renewal_allowed(swept))

        for reason in ("LEFT_CAMPUS", "VERIFICATION_INVALID", "POLICY_VIOLATION", "EMAIL_CHANGED"):
            u = campus_user(f"rev{reason[:4].lower()}{len(reason)}")
            CampusVerification.objects.filter(user=u).update(revoked_at=timezone.now(), revoke_reason=reason)
            self.assertFalse(renewal.renewal_allowed(u), reason)

    def test_other_accounts_cannot(self):
        gmail = create_verified_user("gm", "gm@gmail.com", PW, campus=True)
        self.assertFalse(renewal.renewal_allowed(gmail))  # not a campus address
        never = create_verified_user("never", "never@lpu.co.in", PW)
        self.assertFalse(renewal.renewal_allowed(never))  # never verified
        unverified = User.objects.create_user("unv", "unv@lpu.co.in", PW)
        self.assertFalse(renewal.renewal_allowed(unverified))
        inactive = campus_user("inactive")
        User.objects.filter(pk=inactive.pk).update(is_active=False)
        inactive.refresh_from_db()
        self.assertFalse(renewal.renewal_allowed(inactive))


class RequestAndConfirmTests(TestCase):
    def test_the_link_goes_to_the_campus_address_and_only_a_hash_is_stored(self):
        u = campus_user("stu")
        uid, token = link_for(u)
        [m] = mail.outbox
        self.assertEqual(m.to, ["stu@lpu.co.in"])
        u.refresh_from_db()
        self.assertNotIn(token, u.email_token_hash)
        self.assertEqual(int(uid), u.pk)

    def test_a_new_request_kills_the_previous_link(self):
        u = campus_user("stu")
        _, old = link_for(u)
        _, new = link_for(u)
        with self.assertRaises(InvalidToken):
            renewal.confirm_renewal(u.pk, old)
        renewal.confirm_renewal(u.pk, new)

    def test_a_non_renewable_account_gets_no_email(self):
        u = campus_user("bad")
        CampusVerification.objects.filter(user=u).update(revoked_at=timezone.now(), revoke_reason="POLICY_VIOLATION")
        mail.outbox.clear()
        with self.assertRaises(NotPermitted):
            renewal.request_renewal(u.pk)
        self.assertEqual(mail.outbox, [])

    def test_confirming_renews_keeps_the_binding_and_supersedes_the_old_row(self):
        u = campus_user("stu", age=TTL - timedelta(days=5))  # about to lapse
        old = CampusVerification.objects.get(user=u)
        binding = StudentIdentity.objects.get(user=u)
        _, token = link_for(u)
        renewal.confirm_renewal(u.pk, token)
        old.refresh_from_db()
        self.assertEqual(old.revoke_reason, "SUPERSEDED")
        fresh = CampusVerification.objects.get(user=u, revoked_at__isnull=True)
        self.assertEqual((fresh.method, fresh.verified_email), ("CAMPUS_EMAIL", "stu@lpu.co.in"))
        self.assertGreater(fresh.verified_at, timezone.now() - timedelta(minutes=1))
        self.assertEqual(StudentIdentity.objects.get(user=u), binding)  # binding untouched, no reg no asked
        self.assertEqual(AuditEvent.objects.filter(action="VERIFIED", subject=u).count(), 1)

    def test_a_lapsed_user_regains_access(self):
        u = campus_user("lapsed", age=TTL + timedelta(days=2))
        self.assertFalse(is_campus_verified(u))
        _, token = link_for(u)
        renewal.confirm_renewal(u.pk, token)
        self.assertTrue(is_campus_verified(u))
        self.assertEqual(CampusVerification.objects.filter(user=u, revoked_at__isnull=True).count(), 1)

    def test_the_link_is_single_use(self):
        u = campus_user("stu")
        _, token = link_for(u)
        renewal.confirm_renewal(u.pk, token)
        with self.assertRaises(InvalidToken):
            renewal.confirm_renewal(u.pk, token)
        self.assertEqual(CampusVerification.objects.filter(user=u).count(), 2)

    def test_garbage_expired_and_signup_style_tokens_fail(self):
        u = campus_user("stu")
        _, token = link_for(u)
        for bad in ("nope", token[:-1], ""):
            with self.assertRaises(InvalidToken):
                renewal.confirm_renewal(u.pk, bad)
        User.objects.filter(pk=u.pk).update(email_token_expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(InvalidToken):
            renewal.confirm_renewal(u.pk, token)

    def test_a_password_change_kills_an_outstanding_link(self):
        u = campus_user("stu")
        _, token = link_for(u)
        u.set_password("another-pw-9876")
        u.save()
        with self.assertRaises(InvalidToken):
            renewal.confirm_renewal(u.pk, token)

    def test_revocation_after_the_link_was_sent_blocks_it(self):
        u = campus_user("stu")
        _, token = link_for(u)
        CampusVerification.objects.filter(user=u).update(revoked_at=timezone.now(), revoke_reason="POLICY_VIOLATION")
        with self.assertRaises(InvalidToken):
            renewal.confirm_renewal(u.pk, token)
        self.assertFalse(is_campus_verified(u))


class ViewTests(TestCase):
    def setUp(self):
        cache.clear()
        self.u = campus_user("stu")
        self.client.force_login(self.u)

    def test_login_is_required(self):
        self.client.logout()
        self.assertEqual(self.client.get(reverse("accounts:renew")).status_code, 302)
        self.assertEqual(self.client.post(reverse("accounts:renew")).status_code, 302)

    def test_requesting_sends_one_email_and_redirects_back(self):
        with self.captureOnCommitCallbacks(execute=True):
            r = self.client.post(reverse("accounts:renew"))
        self.assertRedirects(r, reverse("accounts:renew"))
        self.assertEqual(len(mail.outbox), 1)

    def test_requests_are_rate_limited_per_account(self):
        codes = [self.client.post(reverse("accounts:renew")).status_code for _ in range(4)]
        self.assertEqual(codes, [302, 302, 302, 429])

    def test_a_non_renewable_account_sees_guidance_not_a_button(self):
        g = create_verified_user("gm", "gm@gmail.com", PW)
        self.client.force_login(g)
        r = self.client.get(reverse("accounts:renew"))
        self.assertContains(r, "can't be renewed by email")
        self.assertNotContains(r, "EMAIL ME THE LINK")

    def test_get_on_the_link_changes_nothing_and_post_renews(self):
        old_row = CampusVerification.objects.get(user=self.u)
        uid, token = link_for(self.u)
        url = reverse("accounts:renew_confirm", args=[uid, token])
        r = self.client.get(url)
        self.assertContains(r, "RENEW")
        old_row.refresh_from_db()
        self.assertIsNone(old_row.revoked_at)  # a scanner's GET did nothing
        self.assertRedirects(self.client.post(url), reverse("me"))
        old_row.refresh_from_db()
        self.assertEqual(old_row.revoke_reason, "SUPERSEDED")

    def test_someone_elses_link_does_not_work_for_you(self):
        uid, token = link_for(self.u)
        other = campus_user("other")
        self.client.force_login(other)
        url = reverse("accounts:renew_confirm", args=[uid, token])
        self.assertEqual(self.client.get(url).status_code, 400)
        self.assertEqual(self.client.post(url).status_code, 400)
        self.assertEqual(CampusVerification.objects.filter(user=self.u).count(), 1)

    def test_a_used_link_shows_the_invalid_page(self):
        uid, token = link_for(self.u)
        url = reverse("accounts:renew_confirm", args=[uid, token])
        self.client.post(url)
        self.assertEqual(self.client.get(url).status_code, 400)

    def test_the_dashboard_offers_renewal(self):
        self.assertContains(self.client.get(reverse("me")), reverse("accounts:renew"))


class DoubleConfirmRaceTests(TransactionTestCase):
    def test_two_confirmations_of_one_link_renew_exactly_once(self):
        u = campus_user("racer")
        _, token = link_for(u)
        worker, outcome = in_worker(lambda: renewal.confirm_renewal(u.pk, token))
        with transaction.atomic():
            User.objects.select_for_update(no_key=True).get(pk=u.pk)  # hold the account's row
            worker.start()
            wait_until_a_lock_request_is_waiting()
            renewal.confirm_renewal(u.pk, token)  # the first confirmation wins; commits on exit
        worker.join(timeout=30)
        self.assertFalse(worker.is_alive(), "worker hung")
        self.assertIsInstance(outcome.get("err"), InvalidToken, outcome)
        self.assertEqual(CampusVerification.objects.filter(user=u, revoked_at__isnull=True).count(), 1)
        self.assertEqual(CampusVerification.objects.filter(user=u).count(), 2)
