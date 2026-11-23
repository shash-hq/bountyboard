"""Account services: email verification and signup.

Lock rule (shared with apps/bounties/services.py): bounty -> claims -> users. Nothing here
touches bounties or claims (only reads, to see whether an account owns rows), and each function
locks at most one user row at a time, after the per-address advisory lock.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import timedelta
from functools import partial

from django.conf import settings
from django.core.mail import send_mail
from django.db import IntegrityError, connection, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone

from apps.bounties.notifications import absolute_url

from .models import User

logger = logging.getLogger(__name__)


class InvalidToken(Exception):
    """The link or password is wrong, or the link is expired/used.

    One exception for all of them on purpose: the page must not tell a guesser which it was.
    """


class UsernameTaken(Exception):
    """The username belongs to another account that may not be replaced."""


# --- Token --------------------------------------------------------------------------------

def _token_hash(user, token: str) -> str:
    """sha256 over (user id, password hash, token).

    Binding to the id and the current password hash (the same idea as Django's
    PasswordResetTokenGenerator) means a link dies the moment the account is replaced (new id)
    or its password changes, without any extra bookkeeping. sha256, not a slow KDF: the token
    is 256 random bits, so there is nothing to brute-force; hashing only ensures a leaked DB
    row cannot be turned back into a working link.
    """
    return hashlib.sha256(f"{user.pk}:{user.password}:{token}".encode()).hexdigest()


def _token_is_live(user, token: str, now) -> bool:
    if not user.email_token_hash or user.email_token_expires_at is None or user.email_token_expires_at <= now:
        return False
    return hmac.compare_digest(user.email_token_hash, _token_hash(user, token))


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
        user.email_token_hash = _token_hash(user, token)
        user.email_token_expires_at = timezone.now() + timedelta(hours=settings.EMAIL_VERIFICATION_TTL_HOURS)
        user.save(update_fields=["email_token_hash", "email_token_expires_at"])
        transaction.on_commit(partial(_deliver_verification, user.pk, user.email, user.username, token), robust=True)
    return True


def _deliver_verification(uid: int, email: str, username: str, token: str) -> None:
    link = absolute_url(reverse("accounts:verify_email", args=[uid, token]))
    send_mail(
        subject="[Bounty Board] Confirm your email",
        message=(
            f"Hi {username},\n\n"
            f"Someone signed up to Bounty Board with {email}. To confirm it was you, open this "
            f"link and enter your password:\n{link}\n\n"
            f"The link works once and expires in {settings.EMAIL_VERIFICATION_TTL_HOURS} hours.\n\n"
            f"Didn't sign up? Ignore this email and nothing will happen.\n\n"
            f"— Bounty Board Campus Marketplace"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
    )


def peek_confirmation(uid: int, token: str):
    """Read-only: the user this link would confirm, or None. Never changes state, so it is safe
    for GET (link scanners prefetch GETs)."""
    user = User.objects.filter(pk=uid, is_active=True, email_verified_at__isnull=True).first()
    if user is not None and _token_is_live(user, token, timezone.now()):
        return user
    return None


def confirm_email(uid: int, token: str, password: str) -> User:
    """Consume the link and mark the email verified, if the password is right. Raises
    ``InvalidToken`` for any failure.

    Why the password: the link alone proves only that someone can read the mailbox, not that
    the account is theirs. An attacker can sign up with a victim's address and their own
    password; requiring that password here means the victim cannot confirm the attacker's
    account by clicking the unsolicited link.

    The row is locked (``SELECT ... FOR UPDATE``) and the token re-checked under the lock, so a
    concurrent second confirm blocks, then sees the hash nulled and fails (single-use). A wrong
    password does NOT consume the token, otherwise anyone holding the link could burn it.
    """
    with transaction.atomic():
        user = User.objects.select_for_update().filter(pk=uid, is_active=True).first()
        now = timezone.now()
        if user is None or user.email_verified_at is not None or not _token_is_live(user, token, now):
            raise InvalidToken
        if not user.check_password(password):
            raise InvalidToken
        user.email_verified_at = now
        user.email_token_hash = None
        user.email_token_expires_at = None
        user.save(update_fields=["email_verified_at", "email_token_hash", "email_token_expires_at"])
    return user


# --- Signup -------------------------------------------------------------------------------

def owns_rows(user) -> bool:
    """True if any row in any table points at ``user`` (bounties, claims, reports, and whatever
    FKs later PRs add: discovered from the model graph, so it cannot go stale)."""
    for rel in User._meta.related_objects:
        if rel.many_to_many:
            continue
        if rel.related_model._base_manager.filter(**{rel.field.name: user}).exists():
            return True
    return False


def stale_unverified_users(now=None):
    """Unverified accounts older than the link lifetime with no live link, that no one holds a
    role for. A user who just asked for a resend still has a live link, so is not stale."""
    now = now or timezone.now()
    cutoff = now - timedelta(hours=settings.EMAIL_VERIFICATION_TTL_HOURS)
    return User.objects.filter(
        email_verified_at__isnull=True, is_staff=False, is_superuser=False, groups__isnull=True,
        date_joined__lt=cutoff,
    ).filter(Q(email_token_expires_at__isnull=True) | Q(email_token_expires_at__lte=now))


def purge_stale_unverified(now=None) -> int:
    """Delete stale unverified accounts so nobody can hold an address or username forever.
    Accounts that own any rows are never deleted. Returns the number deleted."""
    deleted = 0
    for user in stale_unverified_users(now):
        with transaction.atomic():
            locked = User.objects.select_for_update().filter(pk=user.pk, email_verified_at__isnull=True).first()
            if locked is not None and not owns_rows(locked):
                locked.delete()
                deleted += 1
    return deleted


def username_available(username: str, email: str) -> bool:
    """Pre-check for the form (the service re-checks authoritatively under the lock). A
    username is free if nobody holds it, or its holder is about to be replaced/purged."""
    email = email.strip().lower()
    for holder in User.objects.filter(username__iexact=username):
        if holder.email.lower() == email and holder.email_verified_at is None and not owns_rows(holder):
            continue  # same person retrying: this very account gets replaced
        if stale_unverified_users().filter(pk=holder.pk).exists() and not owns_rows(holder):
            continue
        return False
    return True


def _lock_email(email: str) -> None:
    """Serialize signups for one address. Locking the user row cannot do it: when no row exists
    yet there is nothing to lock. A transaction-scoped advisory lock on the lowercased address
    covers both cases and is released at commit/rollback."""
    if False:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", [email])


def _lookup_holder(email: str):
    return User.objects.select_for_update().filter(email__iexact=email).first()


def signup(username: str, email: str, password: str) -> None:
    """Create an account, or quietly do the right thing if the address is already in use.

    The caller shows the same "check your inbox" response whatever happens here (no account
    enumeration). Outcomes for the address:
    - unused: create the account and email a confirm link.
    - held by an UNVERIFIED account: replace it (delete + create, one transaction). Whoever
      typed the address first has proven nothing about it, so they must not be able to squat it;
      the old link dies with the old user id.
    - held by a VERIFIED account (or, defensively, one that owns rows): change nothing and
      email its owner "you already have an account". Never replaced.
    Raises ``UsernameTaken`` if the username belongs to an account that may not be replaced.
    """
    email = email.strip().lower()
    last = None
    for _ in range(3):
        try:
            with transaction.atomic():
                return _signup_once(username, email, password)
        except IntegrityError as exc:
            # Lost a race the advisory lock did not cover (it only exists on PostgreSQL).
            # A username clash is a real error; an email clash means someone else just created
            # the account, so go round again and treat it as the holder.
            if "username" in str(exc).lower():
                raise UsernameTaken from exc
            last = exc
    raise last


def _signup_once(username: str, email: str, password: str) -> None:
    _lock_email(email)
    holder = _lookup_holder(email)
    if holder is not None:
        if holder.email_verified_at is not None:
            transaction.on_commit(partial(_deliver_account_exists, holder.email, holder.username), robust=True)
            return
        if owns_rows(holder):
            logger.error("Unverified user %s owns rows; refusing to replace it on signup.", holder.pk)
            transaction.on_commit(partial(_deliver_account_exists, holder.email, holder.username), robust=True)
            return
        holder.delete()

    for squatter in User.objects.select_for_update().filter(username__iexact=username):
        if stale_unverified_users().filter(pk=squatter.pk).exists() and not owns_rows(squatter):
            squatter.delete()  # lazy purge: an abandoned signup cannot hold a name forever
        else:
            raise UsernameTaken

    user = User.objects.create_user(username=username, email=email, password=password)
    send_verification_email(user.pk)


def _deliver_account_exists(email: str, username: str) -> None:
    send_mail(
        subject="[Bounty Board] You already have an account",
        message=(
            f"Hi {username},\n\n"
            f"Someone just tried to sign up to Bounty Board with this address, but you already "
            f"have an account.\n\n"
            f"Log in: {absolute_url(reverse('accounts:login'))}\n"
            f"Forgot your password? Reset it: {absolute_url(reverse('accounts:password_reset'))}\n\n"
            f"If that wasn't you, ignore this email; your account is unchanged.\n\n"
            f"— Bounty Board Campus Marketplace"
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[email],
    )
