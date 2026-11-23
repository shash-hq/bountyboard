"""Campus verification: who counts as campus-verified, and the CAMPUS_EMAIL path to become one.

Lock order (see CLAUDE.local.md): bounty -> claims -> user. Everything here runs under the locked
user row that the caller (``services.confirm_email``) already holds, so two concurrent confirms for
one user serialize and the "one active verification per user" index never fires in normal flow.
"""
from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from .identity import InvalidRegNo, hash_reg_no
from .models import (
    AuditEvent, CampusVerification, IdentityReview, IdentitySource, RevokeReason, StudentIdentity,
    VerificationMethod,
)


def campus_email_allowed(email: str) -> bool:
    """True if the address's domain is *exactly* one of ``ALLOWED_CAMPUS_DOMAINS``.

    The domain is everything after the last ``@`` (so ``a@lpu.co.in@evil.com`` is ``evil.com``),
    compared whole and case-insensitively. This replaces the old ``endswith`` check that let
    ``notiitb.ac.in`` pass for ``iitb.ac.in``.
    """
    if not isinstance(email, str) or email.count("@") != 1:
        return False
    local, domain = email.strip().lower().split("@")
    return bool(local) and domain in settings.ALLOWED_CAMPUS_DOMAINS


def active_verifications(now=None):
    """Verifications that currently count: not revoked AND within the TTL.

    Time-based on purpose, not "whatever the sweeper last marked": a sleeping instance or a missed
    tick must not extend anyone's access past the TTL.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(days=settings.VERIFICATION_TTL_DAYS)
    return CampusVerification.objects.filter(revoked_at__isnull=True, verified_at__gt=cutoff)


def is_campus_verified(user, now=None) -> bool:
    return active_verifications(now).filter(user=user).exists()


def needs_reg_no(user) -> bool:
    """Whether confirming this user's email must also collect a registration number."""
    return campus_email_allowed(user.email) and not StudentIdentity.objects.filter(user=user).exists()


def _bind_identity(user, reg_no: str, now):
    """Bind ``reg_no`` to ``user``, or flag a collision for moderators. Never blocks.

    The insert runs in a savepoint. Without it, a unique violation on ``reg_no_hmac`` would abort
    the surrounding transaction (Postgres: "current transaction is aborted") and the verification
    that follows would be lost, so a mistyped or squatted number could lock out the real owner.
    """
    digest, last4 = hash_reg_no(reg_no)
    try:
        with transaction.atomic():
            StudentIdentity.objects.create(
                user=user, reg_no_hmac=digest, reg_no_last4=last4,
                source=IdentitySource.SELF_DECLARED, bound_at=now,
            )
        return
    except IntegrityError:
        pass
    holder = StudentIdentity.objects.filter(reg_no_hmac=digest).first()
    # The user row is locked, so no concurrent confirm can add a second open review for them.
    if not IdentityReview.objects.filter(claimant=user, status="OPEN").exists():
        IdentityReview.objects.create(
            claimant=user, existing_identity=holder, claimed_hmac=digest, claimed_last4=last4,
            claimant_source=IdentitySource.SELF_DECLARED, created_at=now,
        )


def verify_campus_email(user, reg_no, now=None):
    """Create a CAMPUS_EMAIL verification for ``user`` (row must be locked by the caller).

    No-op unless ``user.email`` is an exact campus-domain match. A missing/invalid registration
    number raises ``InvalidRegNo`` *before* anything is written, unless the user already holds a
    binding (renewal), in which case it isn't asked for again.
    """
    if not campus_email_allowed(user.email):
        return None
    now = now or timezone.now()
    has_binding = StudentIdentity.objects.filter(user=user).exists()
    if not has_binding:
        hash_reg_no(reg_no or "")  # validate first; raises InvalidRegNo

    # Close the previous active row (renewal, or lapsed-but-unswept) so the one-active partial
    # unique index stays true and the new row can be inserted.
    CampusVerification.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=now, revoke_reason=RevokeReason.SUPERSEDED
    )
    if not has_binding:
        _bind_identity(user, reg_no, now)
    verification = CampusVerification.objects.create(
        user=user, method=VerificationMethod.CAMPUS_EMAIL, verified_at=now, verified_email=user.email
    )
    AuditEvent.objects.create(
        subject=user, action="VERIFIED", reason=VerificationMethod.CAMPUS_EMAIL,
        verification=verification, created_at=now,
    )
    return verification


__all__ = [
    "InvalidRegNo", "active_verifications", "campus_email_allowed", "is_campus_verified",
    "needs_reg_no", "verify_campus_email",
]
