from django.test import RequestFactory, SimpleTestCase

from config.http import safe_referer

FALLBACK = "/fallback/"


def request(referer=None, secure=False, host="testserver"):
    rf = RequestFactory()
    kwargs = {"HTTP_HOST": host}
    if referer is not None:
        kwargs["HTTP_REFERER"] = referer
    if secure:
        kwargs["secure"] = True
    return rf.post("/x/", **kwargs)


class SafeRefererTests(SimpleTestCase):
    def test_same_host_referer_is_used(self):
        self.assertEqual(safe_referer(request("http://testserver/bounties/5/x/"), FALLBACK),
                         "http://testserver/bounties/5/x/")

    def test_relative_referer_is_used(self):
        self.assertEqual(safe_referer(request("/bounties/mod/"), FALLBACK), "/bounties/mod/")

    def test_missing_referer_falls_back(self):
        self.assertEqual(safe_referer(request(), FALLBACK), FALLBACK)

    def test_foreign_hosts_fall_back(self):
        hostile = [
            "https://evil.example/",
            "https://evil.example/bounties/mod/",          # contains the old substring tests
            "//evil.example/bounties/",                    # protocol-relative
            "https:evil.example",                          # scheme without slashes
            "https://testserver.evil.example/bounties/",   # lookalike suffix
            "https://evil.example\\@testserver/bounties/",  # backslash / userinfo tricks
            "javascript:alert(1)",
            "data:text/html,hi",
            "  https://evil.example/",
        ]
        for url in hostile:
            with self.subTest(url=url):
                self.assertEqual(safe_referer(request(url), FALLBACK), FALLBACK)

    def test_https_request_rejects_a_plain_http_referer(self):
        self.assertEqual(safe_referer(request("http://testserver/bounties/", secure=True), FALLBACK), FALLBACK)
        self.assertEqual(safe_referer(request("https://testserver/bounties/", secure=True), FALLBACK),
                         "https://testserver/bounties/")

    def test_path_prefix_is_enforced(self):
        self.assertEqual(safe_referer(request("http://testserver/me/"), FALLBACK, path_prefix="/bounties/"), FALLBACK)
        self.assertEqual(safe_referer(request("http://testserver/bounties/mod/"), FALLBACK, path_prefix="/bounties/"),
                         "http://testserver/bounties/mod/")
        # the prefix applies to the PATH, not to a substring of the URL
        self.assertEqual(safe_referer(request("http://testserver/me/?x=/bounties/"), FALLBACK, path_prefix="/bounties/"),
                         FALLBACK)
