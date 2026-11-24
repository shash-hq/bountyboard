"""Who may do and see what. The single source of truth for authorization (invariant I6).

Views compute these and pass booleans to templates; templates must not re-implement them.

Roles: a moderator is anyone holding ``bounties.moderate`` (via the Moderators group, or a
superuser). ``is_staff`` only means "may open /admin/" and grants NO moderation power.

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


def can_moderate(user) -> bool:
    """May ``user`` moderate listings and reports?

    ``has_perm`` is True for active superusers and for members of a group holding
    ``bounties.moderate`` (the Moderators group); False for anonymous and inactive users.
    Deliberately does not look at ``is_staff``: that flag is for admin-site access only.
    """
    return bool(getattr(user, "is_authenticated", False)) and user.has_perm("bounties.moderate")


def can_verify_students(user) -> bool:
    """May ``user`` use the verifier desk?

    Holding ``accounts.verify_student`` (the Verifiers group, or a superuser) and nothing else:
    this grants no moderation power and no access to anyone's contact details.
    """
    return bool(getattr(user, "is_authenticated", False)) and user.has_perm("accounts.verify_student")


def can_view_hidden_bounty(user, bounty) -> bool:
    """A hidden bounty is visible only to its poster and to moderators."""
    if not getattr(user, "is_authenticated", False):
        return False
    return user.id == bounty.poster_id or can_moderate(user)


def can_view_claims(user, bounty) -> bool:
    """The full claim list is for the poster and moderators; other users see none.

    Seeing the list is not seeing contact: that stays behind ``can_view_claim_contact``.
    """
    return can_view_hidden_bounty(user, bounty)
