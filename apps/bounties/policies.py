"""Who may see whose contact details. The single source of truth (invariant I6).

Views compute these and pass booleans to templates; templates must not re-implement them.

Rule (decision 3): nothing is revealed before acceptance. After a claim is ACCEPTED, the
poster and that claimant may see each other's contact. Nobody else, including moderators:
moderator access to contact will be a separate, audited action.
"""
from .models import ClaimStatus


def can_view_claim_contact(user, claim) -> bool:
    """May ``user`` see the claimant's contact handle and email on this claim?"""
    if not getattr(user, "is_authenticated", False) or claim.status != ClaimStatus.ACCEPTED:
        return False
    return user.id == claim.bounty.poster_id or user.id == claim.claimant_id


def can_view_poster_contact(user, bounty) -> bool:
    """May ``user`` see the poster's contact? Only the claimant whose claim was accepted."""
    if not getattr(user, "is_authenticated", False):
        return False
    accepted = bounty.accepted_claim
    return accepted is not None and accepted.status == ClaimStatus.ACCEPTED and accepted.claimant_id == user.id
