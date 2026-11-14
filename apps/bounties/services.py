from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction

from .models import Bounty, BountyStatus, Claim, ClaimStatus
from .notifications import (
    send_claim_accepted_notification,
    send_claim_rejected_notification,
)


def accept_claim(claim_id: int, user) -> Claim:
    """
    Atomically accepts one claim and rejects all other pending claims on the bounty.
    Enforces concurrency lock via select_for_update to prevent double-accept races.
    """
    with transaction.atomic():
        try:
            claim = (
                Claim.objects.select_for_update()
                .select_related("bounty")
                .get(id=claim_id)
            )
        except Claim.DoesNotExist:
            raise ValidationError("Claim does not exist.")

        # Lock the bounty row as well
        bounty = (
            Bounty.objects.select_for_update()
            .get(id=claim.bounty_id)
        )

        # Permission check: Only the poster can accept claims
        if user != bounty.poster:
            raise PermissionDenied("Only the bounty poster can accept claims.")

        # Status check
        if bounty.status != BountyStatus.OPEN:
            raise ValidationError(f"Cannot accept claims on a bounty with status '{bounty.status}'.")

        if bounty.is_expired:
            raise ValidationError("Cannot accept claims on an expired bounty.")

        if claim.status != ClaimStatus.PENDING:
            raise ValidationError(f"Cannot accept a claim with status '{claim.status}'.")

        # Update the accepted claim
        claim.status = ClaimStatus.ACCEPTED
        claim.save(update_fields=["status", "updated_at"])

        # Update bounty status and accepted_claim pointer
        bounty.status = BountyStatus.CLAIMED
        bounty.accepted_claim = claim
        bounty.save(update_fields=["status", "accepted_claim", "updated_at"])

        # Reject all other PENDING claims on this bounty
        pending_claims = Claim.objects.filter(
            bounty=bounty, status=ClaimStatus.PENDING
        ).exclude(id=claim.id)
        
        # Keep track of rejected claimants to send notification
        other_claims = list(pending_claims)
        pending_claims.update(status=ClaimStatus.REJECTED)

    # Dispatch notifications after transaction commits
    transaction.on_commit(lambda: send_claim_accepted_notification(claim))
    for other in other_claims:
        transaction.on_commit(lambda c=other: send_claim_rejected_notification(c))

    return claim


def reject_claim(claim_id: int, user) -> Claim:
    """Poster explicitly rejects a pending claim."""
    claim = Claim.objects.select_related("bounty").get(id=claim_id)

    if user != claim.bounty.poster:
        raise PermissionDenied("Only the bounty poster can reject claims.")

    if claim.status != ClaimStatus.PENDING:
        raise ValidationError(f"Cannot reject a claim that is already '{claim.status}'.")

    claim.status = ClaimStatus.REJECTED
    claim.save(update_fields=["status", "updated_at"])

    send_claim_rejected_notification(claim)
    return claim


def withdraw_claim(claim_id: int, user) -> Claim:
    """Claimant withdraws their own pending claim."""
    claim = Claim.objects.select_related("bounty").get(id=claim_id)

    if user != claim.claimant:
        raise PermissionDenied("You can only withdraw your own claims.")

    if claim.status != ClaimStatus.PENDING:
        raise ValidationError(f"Cannot withdraw a claim that is '{claim.status}'.")

    claim.status = ClaimStatus.WITHDRAWN
    claim.save(update_fields=["status", "updated_at"])
    return claim


def mark_bounty_done(bounty_id: int, user) -> Bounty:
    """Poster marks an in-progress claimed bounty as DONE."""
    bounty = Bounty.objects.get(id=bounty_id)

    if user != bounty.poster:
        raise PermissionDenied("Only the bounty poster can mark a bounty as done.")

    if bounty.status != BountyStatus.CLAIMED:
        raise ValidationError("Only claimed bounties can be marked done.")

    bounty.status = BountyStatus.DONE
    bounty.save(update_fields=["status", "updated_at"])
    return bounty


def cancel_bounty(bounty_id: int, user) -> Bounty:
    """Poster cancels an open bounty."""
    bounty = Bounty.objects.get(id=bounty_id)

    if user != bounty.poster:
        raise PermissionDenied("Only the bounty poster can cancel a bounty.")

    if bounty.status != BountyStatus.OPEN:
        raise ValidationError("Only OPEN bounties can be cancelled.")

    bounty.status = BountyStatus.CANCELLED
    # Also reject any pending claims
    bounty.claims.filter(status=ClaimStatus.PENDING).update(status=ClaimStatus.REJECTED)
    bounty.save(update_fields=["status", "updated_at"])
    return bounty
