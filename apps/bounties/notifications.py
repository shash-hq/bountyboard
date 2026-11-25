from django.conf import settings


def absolute_url(path: str) -> str:
    """Absolute link for emails. The host comes from settings.SITE_URL, never from a literal."""
    return settings.SITE_URL.rstrip("/") + path
