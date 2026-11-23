"""Account services: email verification.

Lock rule (shared with apps/bounties/services.py): bounty -> claims -> users. Nothing here
touches bounties or claims, and each function locks at most one user row.
"""
import hashlib
import logging
import secrets
from datetime import timedelta
from functools import partial

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.urls import reverse
from django.utils import timezone

from apps.bounties.notifications import absolute_url

from .models import User

logger = logging.getLogger(__name__)


class InvalidToken(Exception):
    """The verification link is unknown, expired or already used.

    One exception for all three on purpose: the page must not tell a guesser which it was.
    """


def _hash_token(token: str) -> str:
    # sha256, not a slow KDF: the token is 256 random bits, so there is nothing to brute-force;
    # hashing is only so that a leaked DB row cannot be turned back into a working link.
    return hashlib.sha256(token.encode()).hexdigest()


def send_verification_email(user_id: int) -> bool:
    """Issue a fresh token for ``user_id`` and email the link. Returns False if already verified.

    The user row is locked so two concurrent resends serialize: exactly one token is live, and
    the emails go out in commit order. Overwriting the stored hash is what kills the previous
    link. The email is sent on commit, so a rolled-back transaction never mails a token that
    was not saved.
    """
    with transaction.atomic():
        user = User.objects.select_for_update().get(pk=user_id)
        if user.email_verified_at is not None:
            return False
        token = secrets.token_urlsafe(32)
        user.email_token_hash = _hash_token(token)
        user.email_token_expires_at = timezone.now() + timedelta(hours=settings.EMAIL_VERIFICATION_TTL_HOURS)
        user.save(update_fields=["email_token_hash", "email_token_expires_at"])
        transaction.on_commit(partial(_deliver, user.email, user.username, token), robust=True)
    return True


def _deliver(email: str, username: str, token: str) -> None:
    link = absolute_url(reverse("accounts:verify_email", args=[token]))
    send_mail(
        subject="[Bounty Board] Confirm your email",
        message=(
            f"Hi {username},\n\n"
            f"Confirm your email to start using Bounty Board:\n{link}\n\n"
            f"The link works once and expires in {settings.EMAIL_VERIFICATION_TTL_HOURS} hours. "
            f"If you did not sign up, ignore this email and nothing will happen.\n\n"
            f"— Bounty Board Campus Marketplace"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
    )


def confirm_email(token: str) -> User:
    """Consume ``token`` and mark the user's email verified. Raises ``InvalidToken``.

    ``SELECT ... FOR UPDATE`` on the hash lookup makes the token single-use under concurrency:
    a second request blocks until the first commits, then re-evaluates ``WHERE email_token_hash
    = ...`` against the committed row (READ COMMITTED), finds the hash nulled, and fails.
    """
    with transaction.atomic():
        try:
            user = User.objects.select_for_update().get(email_token_hash=_hash_token(token))
        except User.DoesNotExist:
            raise InvalidToken from None
        now = timezone.now()
        if user.email_token_expires_at is None or user.email_token_expires_at <= now:
            raise InvalidToken
        if user.email_verified_at is None:  # idempotent: never move an existing timestamp
            user.email_verified_at = now
        user.email_token_hash = None
        user.email_token_expires_at = None
        user.save(update_fields=["email_verified_at", "email_token_hash", "email_token_expires_at"])
    return user
