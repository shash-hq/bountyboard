from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import Bounty, BountyStatus, Claim, ClaimStatus, Report
from .notifications import (
    send_claim_accepted_notification,
    send_claim_rejected_notification,
)


class InvalidTransition(ValidationError):
    """The requested state change is not valid for the row's CURRENT state.

    Raised after the row lock is taken and the status re-read, so it also covers
    "someone else changed this a moment ago". Subclasses ValidationError so existing
    callers/tests keep working; views read ``.messages`` so users never see ``['...']``.
    """


def _lock_bounty(bounty_id: int) -> Bounty:
    """Lock the bounty row (FIRST in the fixed lock order: bounty -> claims -> user).

    Every transition that touches a bounty or one of its claims takes this lock before
    anything else and re-reads state afterwards, so concurrent transitions on the same
    bounty are serialized and each one validates against the committed result of the last.
    """
    try:
        return Bounty.objects.select_for_update().get(id=bounty_id)
    except Bounty.DoesNotExist:
        raise InvalidTransition("Bounty does not exist.")


def _lock_claim(claim_id: int) -> Claim:
    """Lock a claim row. Callers must already hold its bounty's lock."""
    try:
        return Claim.objects.select_for_update().get(id=claim_id)
    except Claim.DoesNotExist:
        raise InvalidTransition("Claim does not exist.")


def _bounty_id_of_claim(claim_id: int) -> int:
    """Unlocked read of claim.bounty_id. Safe because a claim never changes bounty;
    it only tells us WHICH bounty to lock first (the claim is re-read under the lock)."""
    bounty_id = Claim.objects.filter(id=claim_id).values_list("bounty_id", flat=True).first()
    if bounty_id is None:
        raise InvalidTransition("Claim does not exist.")
    return bounty_id


def create_claim(bounty_id: int, claimant, message: str, contact: str) -> Claim:
    """Submit a claim. Locks the bounty so a claim cannot land on a bounty that was
    accepted/cancelled concurrently, and so a double-submit is detected, not a 500."""
    with transaction.atomic():
        bounty = _lock_bounty(bounty_id)

        if bounty.poster_id == claimant.id:
            raise InvalidTransition("You cannot claim your own bounty.")
        if bounty.status != BountyStatus.OPEN or bounty.is_expired or bounty.is_hidden:
            raise InvalidTransition("This bounty is no longer accepting claims.")
        if Claim.objects.filter(bounty=bounty, claimant=claimant).exists():
            raise InvalidTransition("You have already submitted a claim on this bounty.")

        claim = Claim(bounty=bounty, claimant=claimant, message=message, contact=contact)
        claim.save()
    return claim


def accept_claim(claim_id: int, user) -> Claim:
    """
    Atomically accepts one claim and rejects all other pending claims on the bounty.
    Lock order: bounty, then claims. State is re-validated after the locks are held.
    """
    with transaction.atomic():
        bounty = _lock_bounty(_bounty_id_of_claim(claim_id))
        claim = _lock_claim(claim_id)

        # Permission check: Only the poster can accept claims
        if user != bounty.poster:
            raise PermissionDenied("Only the bounty poster can accept claims.")

        # Status checks, on the freshly locked rows
        if bounty.status != BountyStatus.OPEN:
            raise InvalidTransition(f"Cannot accept claims on a bounty with status '{bounty.status}'.")

        if bounty.is_expired:
            raise InvalidTransition("Cannot accept claims on an expired bounty.")

        if claim.status != ClaimStatus.PENDING:
            raise InvalidTransition(f"Cannot accept a claim with status '{claim.status}'.")

        # Update the accepted claim
        claim.status = ClaimStatus.ACCEPTED
        claim.save(update_fields=["status", "updated_at"])

        # Update bounty status and accepted_claim pointer
        bounty.status = BountyStatus.CLAIMED
        bounty.accepted_claim = claim
        bounty.save(update_fields=["status", "accepted_claim", "updated_at"])

        # Reject all other PENDING claims on this bounty (locked, in id order)
        pending_claims = Claim.objects.select_for_update().filter(
            bounty=bounty, status=ClaimStatus.PENDING
        ).exclude(id=claim.id).order_by("id")

        # Keep track of rejected claimants to send notification
        other_claims = list(pending_claims)
        Claim.objects.filter(id__in=[c.id for c in other_claims]).update(status=ClaimStatus.REJECTED)

    # Dispatch notifications after transaction commits
    transaction.on_commit(lambda: send_claim_accepted_notification(claim))
    for other in other_claims:
        transaction.on_commit(lambda c=other: send_claim_rejected_notification(c))

    return claim


def reject_claim(claim_id: int, user) -> Claim:
    """Poster explicitly rejects a pending claim."""
    with transaction.atomic():
        bounty = _lock_bounty(_bounty_id_of_claim(claim_id))
        claim = _lock_claim(claim_id)

        if user != bounty.poster:
            raise PermissionDenied("Only the bounty poster can reject claims.")

        if claim.status != ClaimStatus.PENDING:
            raise InvalidTransition(f"Cannot reject a claim that is already '{claim.status}'.")

        claim.status = ClaimStatus.REJECTED
        claim.save(update_fields=["status", "updated_at"])

    send_claim_rejected_notification(claim)
    return claim


def withdraw_claim(claim_id: int, user) -> Claim:
    """Claimant withdraws their own pending claim."""
    with transaction.atomic():
        _lock_bounty(_bounty_id_of_claim(claim_id))
        claim = _lock_claim(claim_id)

        if user.id != claim.claimant_id:
            raise PermissionDenied("You can only withdraw your own claims.")

        if claim.status != ClaimStatus.PENDING:
            raise InvalidTransition(f"Cannot withdraw a claim that is '{claim.status}'.")

        claim.status = ClaimStatus.WITHDRAWN
        claim.save(update_fields=["status", "updated_at"])
    return claim


def mark_bounty_done(bounty_id: int, user) -> Bounty:
    """Poster marks an in-progress claimed bounty as DONE."""
    with transaction.atomic():
        bounty = _lock_bounty(bounty_id)

        if user != bounty.poster:
            raise PermissionDenied("Only the bounty poster can mark a bounty as done.")

        if bounty.status != BountyStatus.CLAIMED:
            raise InvalidTransition("Only claimed bounties can be marked done.")

        bounty.status = BountyStatus.DONE
        bounty.save(update_fields=["status", "updated_at"])
    return bounty


def cancel_bounty(bounty_id: int, user) -> Bounty:
    """Poster cancels an open bounty."""
    with transaction.atomic():
        bounty = _lock_bounty(bounty_id)

        if user != bounty.poster:
            raise PermissionDenied("Only the bounty poster can cancel a bounty.")

        if bounty.status != BountyStatus.OPEN:
            raise InvalidTransition("Only OPEN bounties can be cancelled.")

        bounty.status = BountyStatus.CANCELLED
        bounty.save(update_fields=["status", "updated_at"])
        # Also reject any pending claims, in the same transaction, under the bounty lock
        pending_ids = list(
            Claim.objects.select_for_update()
            .filter(bounty=bounty, status=ClaimStatus.PENDING)
            .order_by("id")
            .values_list("id", flat=True)
        )
        Claim.objects.filter(id__in=pending_ids).update(status=ClaimStatus.REJECTED)
    return bounty


def hide_bounty(bounty_id: int, user, is_hidden: bool = True) -> Bounty:
    """Moderator toggles visibility of a bounty."""
    if not (user.is_authenticated and (user.has_perm("bounties.moderate") or user.is_staff)):
        raise PermissionDenied("You do not have permission to moderate bounties.")

    try:
        bounty = Bounty.objects.get(id=bounty_id)
    except Bounty.DoesNotExist:
        raise ValidationError("Bounty does not exist.")

    bounty.is_hidden = is_hidden
    bounty.save(update_fields=["is_hidden", "updated_at"])
    return bounty


def resolve_report(report_id: int, user, hide_bounty_flag: bool = False) -> Report:
    """Moderator resolves an abuse report, optionally hiding the associated bounty."""
    if not (user.is_authenticated and (user.has_perm("bounties.moderate") or user.is_staff)):
        raise PermissionDenied("You do not have permission to moderate reports.")

    try:
        report = Report.objects.select_related("bounty").get(id=report_id)
    except Report.DoesNotExist:
        raise ValidationError("Report does not exist.")

    report.resolved = True
    report.save(update_fields=["resolved"])

    if hide_bounty_flag:
        report.bounty.is_hidden = True
        report.bounty.save(update_fields=["is_hidden", "updated_at"])

    return report
