"""Small HTTP helpers shared by the apps."""
from urllib.parse import urlsplit

from django.utils.http import url_has_allowed_host_and_scheme


def safe_referer(request, fallback, path_prefix=None):
    """Return the Referer if it is safe to redirect back to, else ``fallback``.

    The Referer header is attacker-influenced (a link on another site, or a crafted request),
    so redirecting to it unchecked is an open redirect. A Referer is accepted only when it
    points at THIS host (and https when the request is https), and, if ``path_prefix`` is given,
    at a path under it. A substring test such as ``"bounties" in referer`` is NOT enough:
    ``https://evil.example/bounties`` contains it.
    """
    referer = request.META.get("HTTP_REFERER")
    if not referer:
        return fallback
    if not url_has_allowed_host_and_scheme(
        referer, allowed_hosts={request.get_host()}, require_https=request.is_secure()
    ):
        return fallback
    if path_prefix and not urlsplit(referer).path.startswith(path_prefix):
        return fallback
    return referer
