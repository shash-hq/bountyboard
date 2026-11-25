"""Renewing a campus verification before (or after) it lapses.

Renewal re-proves the campus mailbox: we email a single-use link to the account's campus address,
and following it (while signed in as that account) inserts a fresh CAMPUS_EMAIL row and supersedes the
old one. The identity binding is untouched, so the registration number is not asked for again.

Who may renew: an email-verified, active account whose address is an exact campus-domain match, and
who either holds an active verification or whose latest one simply expired. A verification revoked
for cause (left campus, invalid, policy violation, email changed) cannot be renewed by the user: that
needs a moderator or a desk. There is deliberately no one-click "I'm still on campus" button, which
would make the TTL meaningless.

The token reuses the user's email-token columns (the same hashing as signup confirmation). Those are
otherwise unused once the email is verified, and ``confirm_email`` refuses verified accounts, so the
two flows cannot be crossed.
"""
import secrets
from datetime import timedelta
from functools import partial

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.bounties.exceptions import NotPermitted
from apps.bounties.notifications import absolute_url

from . import campus
from .errors import InvalidState
from .models import CampusVerification, RevokeReason, User
from .services import InvalidToken, _token_hash, _token_is_live

RENEWABLE_ENDINGS = (RevokeReason.EXPIRED,)


def renewal_allowed(user, now=None) -> bool:
    if not (user.is_active and user.email_verified_at is not None and campus.campus_email_allowed(user.email)):
        return False
    now = now or timezone.now()
    if campus.active_verifications(now).filter(user=user).exists():
        return True
    latest = CampusVerification.objects.filter(user=user).order_by("-verified_at", "-pk").first()
    # A row that is unrevoked but past the TTL is a lapse the sweeper hasn't recorded yet.
    return latest is not None and (latest.revoked_at is None or latest.revoke_reason in RENEWABLE_ENDINGS)


def request_renewal(user_id: int) -> None:
    """Issue a fresh single-use link and email it to the account's campus address (on commit).

    The user row is locked, so concurrent requests serialize and only the newest link is live.
    """
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        if not renewal_allowed(user):
            raise NotPermitted("This account can't be renewed by email. Visit a verification desk instead.")
        token = secrets.token_urlsafe(32)
        user.email_token_hash = _token_hash(user, token)
        user.email_token_expires_at = timezone.now() + timedelta(hours=settings.EMAIL_VERIFICATION_TTL_HOURS)
        user.save(update_fields=["email_token_hash", "email_token_expires_at"])
        transaction.on_commit(partial(_deliver, user.pk, user.email, user.username, token), robust=True)


def _deliver(uid, email, username, token) -> None:
    link = absolute_url(reverse("accounts:renew_confirm", args=[uid, token]))
    send_mail(
        subject="[Bounty Board] Renew your campus verification",
        message=(
            f"Hi {username},\n\n"
            "To renew your campus verification, open this link while signed in and press the button:\n"
            f"{link}\n\n"
            f"The link works once and expires in {settings.EMAIL_VERIFICATION_TTL_HOURS} hours.\n"
            "Didn't ask for this? Ignore this email; nothing changes.\n\n"
            "— Bounty Board Campus Marketplace"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
        fail_silently=False,
    )


def peek_renewal(user, token) -> bool:
    """Is this link valid for ``user`` right now? Read-only (GET must never change state)."""
    return _token_is_live(user, token, timezone.now()) and renewal_allowed(user)


def confirm_renewal(user_id: int, token: str, reg_no: str = "", now=None):
    """Consume the link and insert a fresh verification. Raises ``InvalidToken`` for any bad link.

    Runs ``campus.verify_campus_email`` (supersede the active or lapsed-but-unswept row, create the new
    one, audit it) under the user lock, in the same transaction that burns the token. A user who
    somehow has no binding must still supply a registration number, exactly as at signup.
    """
    now = now or timezone.now()
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=user_id, is_active=True).first()
        if user is None or not _token_is_live(user, token, now) or not renewal_allowed(user, now):
            raise InvalidToken
        campus.verify_campus_email(user, reg_no, now)  # may raise InvalidRegNo: nothing written yet
        user.email_token_hash = None
        user.email_token_expires_at = None
        user.save(update_fields=["email_token_hash", "email_token_expires_at"])
    return user
