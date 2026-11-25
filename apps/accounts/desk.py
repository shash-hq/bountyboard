"""In-person verification: a volunteer at a desk sees the physical ID card and marks the student
verified. Least privilege: the verifier needs only ``accounts.verify_student`` and sees only the
username, never the student's email or contact.

Lock order (CLAUDE.local.md): bounty -> claims -> user; users in ascending id order.
"""
import hashlib
import hmac
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import campus
from .errors import (
    InvalidPresenceCode, NotPermitted, SubjectNotFound, VerificationBlocked, VerifierRateLimited,
)
from .identity import encrypt_card_name, hash_reg_no, normalize_card_name, normalize_reg_no
from .models import AuditEvent, CampusVerification, IdentitySource, StudentIdentity, User, VerificationMethod

PRESENCE_WINDOW_SECONDS = 600


def _window(now) -> int:
    return int(now.timestamp()) // PRESENCE_WINDOW_SECONDS


def _code_for(user_id: int, window: int) -> str:
    mac = hmac.new(settings.SECRET_KEY.encode(), f"presence:{user_id}:{window}".encode(), hashlib.sha256).digest()
    return f"{int.from_bytes(mac[:8], 'big') % 1_000_000:06d}"


def presence_code(user_id: int, now=None) -> str:
    """The 6-digit code a student shows at the desk (stateless: nothing is stored).

    Proves the student is present and cooperating, not that they are who they say: that is what
    the card is for. It is bound to the user id, so a code read over someone's shoulder is useless
    for any other account, and it rotates every 10 minutes.
    """
    return _code_for(user_id, _window(now or timezone.now()))


def _code_is_valid(user_id: int, code: str, now) -> bool:
    # Current and previous window, so a code shown just before a boundary still works.
    w = _window(now)
    code = (code or "").strip()
    return any(hmac.compare_digest(code, _code_for(user_id, x)) for x in (w, w - 1))


def _check_rate_limit(verifier, now) -> None:
    """Called with the verifier row locked, so two concurrent verifications by the same verifier
    serialize: both cannot read "29 of 30" and then both insert."""
    done = AuditEvent.objects.filter(actor=verifier, action="VERIFIED", reason=VerificationMethod.IN_PERSON)
    if done.filter(created_at__gt=now - timedelta(hours=1)).count() >= settings.VERIFIER_LIMIT_HOURLY:
        raise VerifierRateLimited("You have reached the hourly verification limit. Try again later.")
    if done.filter(created_at__gt=now - timedelta(days=1)).count() >= settings.VERIFIER_LIMIT_DAILY:
        raise VerifierRateLimited("You have reached the daily verification limit.")


def verify_in_person(verifier_id, username, presence_code_value, reg_no, card_name, now=None):
    now = now or timezone.now()
    # Validate input first: no locks taken for a typo.
    reg_no = normalize_reg_no(reg_no)
    card_name = normalize_card_name(card_name)
    digest, last4 = hash_reg_no(reg_no)

    subject_id = User.objects.filter(username__iexact=(username or "").strip()).values_list("pk", flat=True).first()
    if subject_id is None:
        raise SubjectNotFound("No such user.")
    if subject_id == verifier_id:
        raise NotPermitted("You cannot verify yourself.")

    with transaction.atomic():
        locked = {u.pk: u for u in User.objects.select_for_update(no_key=True).filter(pk__in=[verifier_id, subject_id]).order_by("pk")}
        verifier, subject = locked.get(verifier_id), locked.get(subject_id)
        # Permission is read from the freshly locked row, so a revoked group membership takes effect
        # immediately rather than after a stale cached permission set.
        if verifier is None or not verifier.is_active or not verifier.has_perm("accounts.verify_student"):
            raise NotPermitted("You are not allowed to verify students.")
        if subject is None or not subject.is_active or subject.email_verified_at is None:
            raise SubjectNotFound("No such user.")
        if not _code_is_valid(subject.pk, presence_code_value, now):
            raise InvalidPresenceCode("That code is wrong or has expired. Ask the student to refresh their code.")
        # After the code check, so a verifier learns this only about someone standing in front of them.
        if campus.is_blocked(subject):
            raise VerificationBlocked("This account can't be verified until a moderator allows it.")
        _check_rate_limit(verifier, now)

        identity = _card_identity(subject, digest, last4, now)
        campus.supersede_active(subject, now)
        verification = CampusVerification.objects.create(
            user=subject, method=VerificationMethod.IN_PERSON, verified_at=now, verified_by=verifier,
            identity=identity, card_name_enc=encrypt_card_name(card_name),
        )
        AuditEvent.objects.create(
            actor=verifier, subject=subject, action="VERIFIED", reason=VerificationMethod.IN_PERSON,
            verification=verification, created_at=now,
        )
    return verification


def _card_identity(subject, digest, last4, now):
    """Bind (or confirm) the card's registration number for ``subject``. Never blocks.

    - No binding yet: bind as CARD_CHECKED (a collision opens a review, identity stays NULL).
    - Same number already self-declared: upgrade it to CARD_CHECKED (a card outranks a typed number).
    - A *different* number is already bound to this user: a human saw a card that disagrees with what
      the student typed. Which one is right is a moderator's call, so the existing binding is left
      untouched, the verification still goes through (the card is what a trusted person saw), and a
      review is opened.
    """
    existing = StudentIdentity.objects.filter(user=subject).first()
    if existing is None:
        return campus.bind_identity(subject, digest, last4, IdentitySource.CARD_CHECKED, now)
    if existing.reg_no_hmac == digest:
        if existing.source != IdentitySource.CARD_CHECKED:
            existing.source = IdentitySource.CARD_CHECKED
            existing.save(update_fields=["source"])
        return existing
    holder = StudentIdentity.objects.filter(reg_no_hmac=digest).first()
    campus.open_review(subject, holder or existing, digest, last4, IdentitySource.CARD_CHECKED, now)
    return None
