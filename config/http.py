"""Small HTTP helpers shared by the apps."""
import ipaddress
from urllib.parse import urlsplit

from django.conf import settings
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


def client_ip(request):
    """The client's IP address, for rate limiting.

    Behind a reverse proxy REMOTE_ADDR is the proxy, so every user would share one bucket.
    With ``TRUSTED_PROXY_COUNT`` = N proxies we trust, each appends the address it saw to
    X-Forwarded-For, so the real client is the N-th entry FROM THE RIGHT. Entries further left
    are client-supplied and ignored, so spoofing the header gains nothing. With N = 0, or a
    header shorter than N, or an unparsable entry, fall back to REMOTE_ADDR.
    """
    remote = request.META.get("REMOTE_ADDR", "")
    hops = getattr(settings, "TRUSTED_PROXY_COUNT", 0)
    if hops <= 0:
        return remote
    parts = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
    if len(parts) < hops:
        return remote
    candidate = parts[-hops]
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return remote
    return candidate
