"""Rate-limit keys and rates for the account views (django-ratelimit callables)."""
from django.conf import settings


def _normalized(request, field):
    value = (request.POST.get(field) or "").strip().lower()[:254]
    return value or "-"  # an empty identifier still lands in ONE shared bucket


def login_identifier(group, request):
    """Per account: the submitted username-or-email, so guessing one account's password is
    limited no matter how many IPs the guesses come from."""
    return _normalized(request, "username_or_email")


def confirm_account(group, request):
    """Per account, for the confirm-email password check: the username from the link's user id,
    lowercased, i.e. the same bucket a username login attempt for that account uses. An unknown
    id still gets its own bucket so guessing ids is throttled too."""
    from django.contrib.auth import get_user_model

    uid = request.resolver_match.kwargs.get("uid")
    username = get_user_model().objects.filter(pk=uid).values_list("username", flat=True).first()
    return (username or f"uid:{uid}").lower()[:254]


def reset_email(group, request):
    """Per target mailbox: caps how many reset emails one address can be sent (email is scarce)."""
    return _normalized(request, "email")


def rate(name):
    """A callable rate so the value is read from settings at request time (overridable in tests)."""
    return lambda group, request: settings.RATE_LIMITS[name]
