"""Rate-limit keys and rates for the account views (django-ratelimit callables)."""
from django.conf import settings


def _normalized(request, field):
    value = (request.POST.get(field) or "").strip().lower()[:254]
    return value or "-"  # an empty identifier still lands in ONE shared bucket


def _account_bucket(user_pk, unresolved):
    """The ONE per-account failure bucket, shared by login and confirm. A resolved account is
    keyed on its primary key, so every way of naming it (username, email in any case, the
    confirm link's uid) spends the same budget. A name that resolves to nothing still gets a
    bucket (keyed on the normalized text), so the limit behaves identically whether or not the
    account exists and guessing names is throttled too."""
    return f"user:{user_pk}" if user_pk is not None else f"unresolved:{unresolved}"


def login_identifier(group, request):
    """Per account for login: resolve the submitted username-or-email to a user pk, in the same
    order LoginForm tries them (username, then email). Both lookups always run, so a known and an
    unknown identifier cost the same."""
    from django.contrib.auth import get_user_model

    ident = _normalized(request, "username_or_email")
    users = get_user_model().objects
    by_username = users.filter(username__iexact=ident).values_list("pk", flat=True).first()
    by_email = users.filter(email__iexact=ident).values_list("pk", flat=True).first()
    pk = by_username if by_username is not None else by_email
    return _account_bucket(pk, ident)


def confirm_account(group, request):
    """Per account for the confirm-email password check: the link's user id, which is already
    the pk, in the same bucket a login for that account uses."""
    from django.contrib.auth import get_user_model

    uid = str(request.resolver_match.kwargs.get("uid"))
    pk = get_user_model().objects.filter(pk=uid).values_list("pk", flat=True).first() if uid.isdigit() else None
    return _account_bucket(pk, f"uid:{uid[:40]}")


def reset_email(group, request):
    """Per target mailbox: caps how many reset emails one address can be sent (email is scarce)."""
    return _normalized(request, "email")


def desk_verifier(group, request):
    """Per verifier account, so a compromised verifier cannot brute-force 6-digit presence codes."""
    return f"verifier:{request.user.pk}"


def rate(name):
    """A callable rate so the value is read from settings at request time (overridable in tests)."""
    return lambda group, request: settings.RATE_LIMITS[name]
