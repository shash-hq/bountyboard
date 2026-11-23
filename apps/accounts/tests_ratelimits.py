"""Behavioural rate-limit tests: the (N+1)th request really is refused, with 429."""
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from config.http import client_ip

from .testing import create_verified_user

User = get_user_model()
PW = "Correct-horse-9"
GENEROUS = {
    "login_failures_per_account": "1000/h",
    "login_per_ip": "1000/h",
    "signup_per_ip": "1000/h",
    "password_reset_per_email": "1000/h",
    "password_reset_per_ip": "1000/h",
    "verify_resend_per_email": "1000/h",
    "verify_resend_per_ip": "1000/h",
}


def limits(**overrides):
    return override_settings(RATE_LIMITS={**GENEROUS, **overrides})


class RateLimitTestBase(TestCase):
    def setUp(self):
        cache.clear()  # LocMem counters are process-wide and would leak between tests
        self.alice = create_verified_user("alice", "alice@example.org", PW)
        self.bob = create_verified_user("bob", "bob@example.org", PW)

    def login(self, who="alice", password="wrong-password", ip="10.0.0.1", **extra):
        return self.client.post(
            reverse("accounts:login"), {"username_or_email": who, "password": password},
            REMOTE_ADDR=ip, **extra)


class LoginFailureLimitTests(RateLimitTestBase):
    @limits(login_failures_per_account="3/15m")
    def test_repeated_failures_for_one_account_end_in_429(self):
        statuses = [self.login("alice").status_code for _ in range(8)]
        self.assertEqual(statuses[0], 200)         # a normal failed login re-renders the form
        self.assertIn(429, statuses)               # ...and eventually the account is throttled
        first_429 = statuses.index(429)
        self.assertTrue(all(s == 429 for s in statuses[first_429:]))

    @limits(login_failures_per_account="3/15m")
    def test_a_throttled_account_refuses_even_the_correct_password(self):
        for _ in range(8):
            self.login("alice")
        self.assertEqual(self.login("alice", PW).status_code, 429)

    @limits(login_failures_per_account="3/15m")
    def test_other_accounts_on_the_same_ip_are_unaffected(self):
        for _ in range(8):
            self.login("alice")
        self.assertEqual(self.login("bob", PW).status_code, 302)

    @limits(login_failures_per_account="3/15m")
    def test_failures_from_many_ips_still_count_against_the_account(self):
        statuses = [self.login("alice", ip=f"10.0.1.{i}").status_code for i in range(1, 9)]
        self.assertIn(429, statuses)

    @limits(login_failures_per_account="3/15m")
    def test_account_identifier_is_normalized(self):
        statuses = [self.login(v).status_code for v in ["alice", "ALICE", " Alice ", "alice", "ALICE", "alice"] * 2]
        self.assertIn(429, statuses)

    @limits(login_failures_per_account="3/15m")
    def test_successful_logins_never_count(self):
        for _ in range(8):
            r = self.login("alice", PW)
            self.assertEqual(r.status_code, 302)
            self.client.post(reverse("accounts:logout"))

    @limits(login_per_ip="3/h")
    def test_per_ip_ceiling_limits_one_address_but_not_another(self):
        codes = [self.login("alice", PW if i == 0 else "x", ip="10.0.2.1").status_code for i in range(5)]
        self.assertIn(429, codes)
        self.client.logout()
        self.assertNotEqual(self.login("bob", PW, ip="10.0.2.2").status_code, 429)


class SignupLimitTests(RateLimitTestBase):
    @limits(signup_per_ip="2/h")
    def test_signup_is_limited_per_ip(self):
        def signup(ip):
            return self.client.post(reverse("accounts:signup"), {"username": "x"}, REMOTE_ADDR=ip).status_code
        codes = [signup("10.0.3.1") for _ in range(5)]
        self.assertEqual(codes[0], 200)
        self.assertIn(429, codes)
        self.assertEqual(signup("10.0.3.2"), 200)


class PasswordResetLimitTests(RateLimitTestBase):
    def reset(self, email, ip="10.0.4.1"):
        return self.client.post(reverse("accounts:password_reset"), {"email": email}, REMOTE_ADDR=ip)

    @limits(password_reset_per_email="2/h")
    def test_one_mailbox_cannot_be_flooded(self):
        codes = [self.reset("alice@example.org", ip=f"10.0.5.{i}").status_code for i in range(1, 7)]
        self.assertIn(429, codes)
        self.assertLessEqual(len(mail.outbox), 3)  # the budget, not one email per request
        self.assertNotEqual(self.reset("bob@example.org", ip="10.0.5.99").status_code, 429)

    @limits(password_reset_per_email="2/h")
    def test_unknown_addresses_are_limited_identically(self):
        codes = [self.reset("nobody@example.org", ip=f"10.0.6.{i}").status_code for i in range(1, 7)]
        self.assertIn(429, codes)  # no difference in behaviour reveals whether an account exists

    @limits(password_reset_per_ip="2/h")
    def test_one_ip_cannot_flood_many_mailboxes(self):
        codes = [self.reset(f"user{i}@example.org").status_code for i in range(6)]
        self.assertIn(429, codes)
        self.assertNotEqual(self.reset("bob@example.org", ip="10.0.4.2").status_code, 429)


class VerifyResendLimitTests(RateLimitTestBase):
    def setUp(self):
        super().setUp()
        self.carol = User.objects.create_user("carol", "carol@example.org", PW)  # unverified

    def resend(self, email, ip="10.0.8.1"):
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("accounts:resend_verification"), {"email": email}, REMOTE_ADDR=ip)

    @limits(verify_resend_per_email="2/h")
    def test_one_mailbox_cannot_be_flooded(self):
        mail.outbox.clear()
        codes = [self.resend("carol@example.org", ip=f"10.0.9.{i}").status_code for i in range(1, 7)]
        self.assertIn(429, codes)
        self.assertLessEqual(len(mail.outbox), 3)  # the budget, not one email per request
        self.assertNotEqual(self.resend("bob@example.org", ip="10.0.9.99").status_code, 429)

    @limits(verify_resend_per_email="2/h")
    def test_unknown_addresses_are_limited_identically(self):
        codes = [self.resend("nobody@example.org", ip=f"10.0.10.{i}").status_code for i in range(1, 7)]
        self.assertIn(429, codes)

    @limits(verify_resend_per_ip="2/h")
    def test_one_ip_cannot_flood_many_mailboxes(self):
        codes = [self.resend(f"user{i}@example.org").status_code for i in range(6)]
        self.assertIn(429, codes)
        self.assertNotEqual(self.resend("carol@example.org", ip="10.0.8.2").status_code, 429)


class Response429Tests(RateLimitTestBase):
    @limits(login_per_ip="1/h")
    def test_limited_requests_get_429_not_403(self):
        codes = [self.login("alice", ip="10.0.7.1").status_code for _ in range(4)]
        limited = [c for c in codes if c >= 400]
        self.assertTrue(limited)
        self.assertEqual(set(limited), {429})
        r = self.login("alice", ip="10.0.7.1")
        self.assertContains(r, "429", status_code=429)


class ClientIpTests(SimpleTestCase):
    def ip(self, xff=None, remote="10.9.9.9"):
        extra = {"REMOTE_ADDR": remote}
        if xff is not None:
            extra["HTTP_X_FORWARDED_FOR"] = xff
        return client_ip(RequestFactory().get("/", **extra))

    @override_settings(TRUSTED_PROXY_COUNT=0)
    def test_forwarded_header_is_ignored_when_no_proxy_is_trusted(self):
        self.assertEqual(self.ip("1.2.3.4"), "10.9.9.9")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_one_trusted_proxy_uses_the_last_entry(self):
        self.assertEqual(self.ip("1.2.3.4"), "1.2.3.4")
        self.assertEqual(self.ip("6.6.6.6, 1.2.3.4"), "1.2.3.4")  # a client-prefixed fake is ignored

    @override_settings(TRUSTED_PROXY_COUNT=2)
    def test_two_trusted_proxies_use_the_second_from_the_right(self):
        self.assertEqual(self.ip("6.6.6.6, 1.2.3.4, 10.1.1.1"), "1.2.3.4")

    @override_settings(TRUSTED_PROXY_COUNT=2)
    def test_a_header_shorter_than_the_chain_falls_back_to_remote_addr(self):
        self.assertEqual(self.ip("1.2.3.4"), "10.9.9.9")
        self.assertEqual(self.ip(None), "10.9.9.9")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_garbage_entry_falls_back_to_remote_addr(self):
        self.assertEqual(self.ip("not-an-ip"), "10.9.9.9")
        self.assertEqual(self.ip("1.2.3.4, <script>"), "10.9.9.9")

    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_ipv6_client(self):
        self.assertEqual(self.ip("2001:db8::1"), "2001:db8::1")


class ProxyBucketTests(RateLimitTestBase):
    @limits(login_per_ip="2/h")
    @override_settings(TRUSTED_PROXY_COUNT=1)
    def test_clients_behind_one_proxy_get_separate_buckets(self):
        def attempt(client_addr):
            return self.login("alice", "x", ip="10.0.8.1", HTTP_X_FORWARDED_FOR=client_addr).status_code
        self.assertIn(429, [attempt("1.1.1.1") for _ in range(5)])
        self.assertNotEqual(attempt("2.2.2.2"), 429)  # a different student behind the same proxy

    @limits(login_per_ip="2/h")
    @override_settings(TRUSTED_PROXY_COUNT=0)
    def test_spoofing_the_header_does_not_evade_the_limit_when_no_proxy_is_trusted(self):
        codes = [self.login("alice", "x", ip="10.0.9.1", HTTP_X_FORWARDED_FOR=f"9.9.9.{i}").status_code
                 for i in range(6)]
        self.assertIn(429, codes)
