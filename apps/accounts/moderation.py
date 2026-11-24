"""Moderator actions on verification. Every one locks the affected user row, re-reads state under
the lock, and writes exactly one AuditEvent in the same transaction, so there is no privileged
action without a record and no record of an action that rolled back.

Lock order (CLAUDE.local.md): bounty -> claims -> user; users ascending by id. ``accept_claim``
(PR 6) locks the same user rows, which is what makes accept and revoke serialize: either the accept
sees the verification already revoked and fails, or the revoke waits until the accept commits.
"""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.bounties import inbox
from apps.bounties.models import Bounty, BountyStatus, NotificationKind
from apps.bounties.policies import CANCELLABLE_REVOKE_REASONS, can_moderate

from .errors import InvalidState, NotPermitted
from .identity import decrypt_card_name
from .models import AuditEvent, CampusVerification, RevokeReason, StudentIdentity, User, VerificationMethod

# Reasons a moderator may give. EMAIL_CHANGED / EXPIRED / SUPERSEDED are system-only.
MODERATOR_REASONS = (RevokeReason.LEFT_CAMPUS, RevokeReason.VERIFICATION_INVALID, RevokeReason.POLICY_VIOLATION)
MAX_NOTE = 500


def _lock(actor_id, subject_id):
    """Lock actor and subject together in ascending id order. Returns (actor, subject)."""
    rows = {u.pk: u for u in User.objects.select_for_update().filter(pk__in={actor_id, subject_id}).order_by("pk")}
    actor, subject = rows.get(actor_id), rows.get(subject_id)
    # Permission comes from the freshly locked row, so a just-removed Moderators membership applies.
    if actor is None or not can_moderate(actor):
        raise NotPermitted("You are not allowed to moderate.")
    if subject is None:
        raise InvalidState("No such user.")
    return actor, subject


def _clean_note(note: str) -> str:
    note = (note or "").strip()
    if len(note) > MAX_NOTE:
        raise InvalidState(f"Note is too long (max {MAX_NOTE} characters).")
    return note


def revoke_verification(moderator_id, user_id, reason, note="", now=None):
    """Revoke the user's active campus verification. The identity binding is kept, so a revoked
    user cannot simply re-verify the same card on a new account (see ``release_identity``).

    Existing CLAIMED bounties and contact details already shown are left alone. For an invalid
    verification or a policy violation the counterparty of each such bounty gets an in-app
    notification (same transaction, so it exists iff the revoke committed) and may then cancel via
    ``services.cancel_after_counterparty_revocation``. LEFT_CAMPUS notifies nobody.

    Lock order: this takes only user locks and reads bounties without locking them, so it never
    holds a user lock while waiting for a bounty lock (bounty -> claims -> user stays intact). A
    concurrent accept needs the subject's user lock, so it either commits first (and is found by the
    query below) or waits and then fails the verification check.
    """
    if reason not in MODERATOR_REASONS:
        raise InvalidState("Choose a valid revocation reason.")
    note = _clean_note(note)
    now = now or timezone.now()
    with transaction.atomic():
        moderator, subject = _lock(moderator_id, user_id)
        verification = CampusVerification.objects.filter(user=subject, revoked_at__isnull=True).first()
        if verification is None:
            raise InvalidState("This user has no active verification.")
        verification.revoked_at, verification.revoked_by = now, moderator
        verification.revoke_reason, verification.revoke_note = reason, note
        verification.save(update_fields=["revoked_at", "revoked_by", "revoke_reason", "revoke_note"])
        AuditEvent.objects.create(
            actor=moderator, subject=subject, action="REVOKED", reason=reason, note=note,
            verification=verification, created_at=now,
        )
        if reason in CANCELLABLE_REVOKE_REASONS:
            _notify_counterparties(subject, verification)
    return verification


def _notify_counterparties(subject, verification):
    """One notification per CLAIMED bounty the subject is a party to, keyed by (revocation, bounty)."""
    claimed = (
        Bounty.objects.filter(status=BountyStatus.CLAIMED)
        .filter(Q(poster=subject) | Q(accepted_claim__claimant=subject))
        .select_related("poster", "accepted_claim__claimant")
        .order_by("pk")
    )
    for bounty in claimed:
        other = bounty.accepted_claim.claimant if bounty.poster_id == subject.pk else bounty.poster
        inbox.notify(
            other, NotificationKind.COUNTERPARTY_REVOKED,
            f"{subject.username}'s campus verification was revoked, so '{bounty.title}' can no longer "
            "rely on it. You can cancel this bounty from its page.",
            idempotency_key=f"revoked:{verification.pk}:{bounty.pk}", bounty=bounty,
        )


def release_identity(moderator_id, user_id, note, now=None):
    """Free the user's registration-number binding so the number can be bound again.

    For when someone verified with another student's number. It does not revoke the verification:
    that is a separate decision (``revoke_verification``). A note is required, since this is the one
    action that lowers the Sybil guard.
    """
    note = _clean_note(note)
    if not note:
        raise InvalidState("Say why the identity is being released.")
    now = now or timezone.now()
    with transaction.atomic():
        moderator, subject = _lock(moderator_id, user_id)
        identity = StudentIdentity.objects.filter(user=subject).first()
        if identity is None:
            raise InvalidState("This user has no registration-number binding.")
        identity.delete()  # verification.identity / review.existing_identity go NULL (SET_NULL)
        AuditEvent.objects.create(
            actor=moderator, subject=subject, action="IDENTITY_RELEASED", note=note, created_at=now,
        )


def reveal_card_name(actor_id, user_id, reason="", now=None) -> str:
    """The name printed on the user's ID card (their latest in-person check).

    A moderator must give a reason, and the view is audited in the same transaction BEFORE the
    plaintext is returned. The owner may read their own, unaudited. Nobody else can.
    """
    now = now or timezone.now()
    reason = _clean_note(reason)
    with transaction.atomic():
        actor, subject = _lock_any(actor_id, user_id)
        is_owner = actor.pk == subject.pk
        if not is_owner:
            if not can_moderate(actor):
                raise NotPermitted("You are not allowed to view this.")
            if not reason:
                raise InvalidState("Say why you need the card name.")
        # defer(None) clears the manager's defer("card_name_enc"): the one place it is loaded.
        verification = (
            CampusVerification.objects.defer(None)
            .filter(user=subject, method=VerificationMethod.IN_PERSON)
            .order_by("-verified_at", "-pk").first()
        )
        if verification is None or not verification.card_name_enc:
            raise InvalidState("No card name on file.")
        if not is_owner:
            AuditEvent.objects.create(
                actor=actor, subject=subject, action="CARD_NAME_VIEWED", reason="", note=reason,
                verification=verification, created_at=now,
            )
        return decrypt_card_name(verification.card_name_enc)


def _lock_any(actor_id, subject_id):
    rows = {u.pk: u for u in User.objects.select_for_update().filter(pk__in={actor_id, subject_id}).order_by("pk")}
    actor, subject = rows.get(actor_id), rows.get(subject_id)
    if actor is None or not actor.is_active:
        raise NotPermitted("You are not allowed to view this.")
    if subject is None:
        raise InvalidState("No such user.")
    return actor, subject
