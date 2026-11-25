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
from .identity import decrypt_card_name, hash_reg_no
from .models import (
    AuditEvent, CampusVerification, IdentityReview, ReviewResolution, ReviewStatus, RevokeReason,
    StaffSource, StudentIdentity, User, VerificationMethod,
)

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
        return _revoke_locked(moderator, subject, reason, note, now)


def _revoke_locked(moderator, subject, reason, note, now):
    """Revoke ``subject``'s active verification. The caller holds the moderator's and subject's user
    locks, inside one transaction. Writes the AuditEvent and the counterparty notifications."""
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


def _require_moderator_and_reason(actor_id, reason):
    reason = _clean_note(reason)
    if not reason:
        raise InvalidState("Say why you need this.")
    return reason


def lookup_by_reg_no(actor_id, raw_reg_no, reason, now=None):
    """Which account, if any, is bound to this registration number?

    For a moderator who has the number in front of them (a dispute, an identity review). The input
    is hashed with the server key and compared with ``StudentIdentity.reg_no_hmac``; nothing is
    ever stored in the clear and the result carries only the account and the last 4 digits. Every
    lookup is audited, hit or miss, BEFORE the result is returned: a moderator cannot probe
    numbers without leaving a trail. A malformed number raises ``InvalidRegNo`` and is not audited
    (nothing was looked up).
    """
    reason = _require_moderator_and_reason(actor_id, reason)
    digest, _last4 = hash_reg_no(raw_reg_no)
    now = now or timezone.now()
    with transaction.atomic():
        actor = User.objects.select_for_update().filter(pk=actor_id).first()
        if actor is None or not can_moderate(actor):
            raise NotPermitted("You are not allowed to moderate.")
        identity = StudentIdentity.objects.select_related("user").filter(reg_no_hmac=digest).first()
        AuditEvent.objects.create(
            actor=actor, subject=identity.user if identity else None, action="REG_NO_LOOKUP",
            reason="MATCH" if identity else "NO_MATCH", note=reason, created_at=now,
        )
        return identity


def reveal_contact_for_moderation(actor_id, user_id, reason, now=None):
    """The user's email and contact handle, for a moderator who needs to reach them.

    Moderators otherwise never see contact details (``can_view_contact`` excludes them). A reason
    is required, and the AuditEvent is written in the same transaction BEFORE the values are
    returned, so there is no view without a record and no record of a view that rolled back.
    Returns ``(email, contact_handle)``.
    """
    reason = _require_moderator_and_reason(actor_id, reason)
    now = now or timezone.now()
    with transaction.atomic():
        actor, subject = _lock(actor_id, user_id)
        AuditEvent.objects.create(
            actor=actor, subject=subject, action="CONTACT_VIEWED", reason="", note=reason, created_at=now,
        )
        return subject.email, subject.contact_handle


def _lock_many(actor_id, *subject_ids):
    """Lock the moderator and any number of other users together, ascending id. Returns (actor, {pk: user})."""
    ids = {actor_id, *[i for i in subject_ids if i is not None]}
    rows = {u.pk: u for u in User.objects.select_for_update().filter(pk__in=ids).order_by("pk")}
    actor = rows.get(actor_id)
    if actor is None or not can_moderate(actor):
        raise NotPermitted("You are not allowed to moderate.")
    return actor, rows


def resolve_identity_review(moderator_id, review_id, resolution, note, now=None):
    """Close an open registration-number collision.

    ``KEEP_EXISTING``  the existing holder is right; the newcomer's verification is revoked with
                       VERIFICATION_INVALID (so they must verify again properly).
    ``REASSIGN``       the newcomer is right: the holder's binding is released, rebound to the
                       newcomer, and the old holder's verification is revoked (INVALID).
    ``BOTH_LEGIT_TYPO`` nobody is wrongly bound; the review is closed and nothing else changes.

    Runs in one transaction with the users locked in ascending id (moderator, claimant and the
    current holder of the number), then the review re-read under its own lock, so two moderators
    resolving the same review serialize and the second sees it already resolved. Every consequence
    writes its own AuditEvent, plus an IDENTITY_REVIEW_RESOLVED row for the decision itself.
    """
    if resolution not in ReviewResolution.values:
        raise InvalidState("Choose a valid resolution.")
    note = _clean_note(note)
    if not note:
        raise InvalidState("Say why you decided this.")
    now = now or timezone.now()
    with transaction.atomic():
        # Which users to lock: an unlocked peek that is re-validated under the locks below.
        peek = IdentityReview.objects.filter(pk=review_id).values_list("claimant_id", "claimed_hmac").first()
        if peek is None:
            raise InvalidState("No such review.")
        holder_id = StudentIdentity.objects.filter(reg_no_hmac=peek[1]).values_list("user_id", flat=True).first()
        actor, users = _lock_many(moderator_id, peek[0], holder_id)

        review = IdentityReview.objects.select_for_update().filter(pk=review_id).first()
        if review is None or review.status != ReviewStatus.OPEN:
            raise InvalidState("This review was already resolved.")
        claimant = users[review.claimant_id]
        holder_identity = StudentIdentity.objects.filter(reg_no_hmac=review.claimed_hmac).first()
        if (holder_identity.user_id if holder_identity else None) != holder_id:
            raise InvalidState("The binding changed while you were reviewing. Reload and try again.")
        holder = users[holder_id] if holder_id else None

        if resolution == ReviewResolution.KEEP_EXISTING:
            if CampusVerification.objects.filter(user=claimant, revoked_at__isnull=True).exists():
                _revoke_locked(actor, claimant, RevokeReason.VERIFICATION_INVALID, note, now)
        elif resolution == ReviewResolution.REASSIGN:
            if StudentIdentity.objects.filter(user=claimant).exists():
                raise InvalidState("The newcomer already has a registration-number binding.")
            if holder_identity is not None:
                holder_identity.delete()
                AuditEvent.objects.create(
                    actor=actor, subject=holder, action="IDENTITY_RELEASED", note=note, created_at=now
                )
                if CampusVerification.objects.filter(user=holder, revoked_at__isnull=True).exists():
                    _revoke_locked(actor, holder, RevokeReason.VERIFICATION_INVALID, note, now)
            identity = StudentIdentity.objects.create(
                user=claimant, reg_no_hmac=review.claimed_hmac, reg_no_last4=review.claimed_last4,
                source=review.claimant_source, bound_at=now,
            )
            CampusVerification.objects.filter(user=claimant, revoked_at__isnull=True).update(identity=identity)

        review.status, review.resolution = ReviewStatus.RESOLVED, resolution
        review.resolved_by, review.resolved_at, review.note = actor, now, note
        review.save(update_fields=["status", "resolution", "resolved_by", "resolved_at", "note"])
        AuditEvent.objects.create(
            actor=actor, subject=claimant, action="IDENTITY_REVIEW_RESOLVED", reason=resolution, note=note,
            created_at=now,
        )
    return review


def grant_staff(moderator_id, user_id, note="", now=None):
    """Give the staff role (faculty/staff may post tasks) to an account. The badge itself still
    requires an active campus verification where it is rendered; this only records the role."""
    note = _clean_note(note)
    now = now or timezone.now()
    with transaction.atomic():
        moderator, subject = _lock(moderator_id, user_id)
        if subject.staff_source is not None:
            raise InvalidState("This account already has the staff role.")
        subject.staff_since, subject.staff_source, subject.staff_granted_by = now, StaffSource.MODERATOR, moderator
        subject.save(update_fields=["staff_since", "staff_source", "staff_granted_by"])
        AuditEvent.objects.create(
            actor=moderator, subject=subject, action="STAFF_GRANTED", reason=StaffSource.MODERATOR, note=note,
            created_at=now,
        )


def revoke_staff(moderator_id, user_id, note="", now=None):
    """Remove the staff role. An allowlisted address is not re-granted by this: the allowlist is
    consulted only when an email is confirmed."""
    note = _clean_note(note)
    now = now or timezone.now()
    with transaction.atomic():
        moderator, subject = _lock(moderator_id, user_id)
        if subject.staff_source is None:
            raise InvalidState("This account doesn't have the staff role.")
        was = subject.staff_source
        subject.staff_since = subject.staff_source = subject.staff_granted_by = None
        subject.save(update_fields=["staff_since", "staff_source", "staff_granted_by"])
        AuditEvent.objects.create(
            actor=moderator, subject=subject, action="STAFF_REVOKED", reason=was, note=note, created_at=now,
        )
