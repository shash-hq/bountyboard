"""Who may do and see what. The single source of truth for authorization (invariant I6).

Views compute these and pass booleans to templates; templates must not re-implement them.

Roles: a moderator is anyone holding ``bounties.moderate`` (via the Moderators group, or a
superuser). ``is_staff`` only means "may open /admin/" and grants NO moderation power.

Rule (decision 3): nothing is revealed before acceptance. After a claim is ACCEPTED, the
poster and that claimant may see each other's contact (profile email + contact handle). Nobody
else, including moderators: moderator access to contact will be a separate, audited action.
"""
from django.conf import settings

from apps.accounts import campus

from .models import ClaimStatus


def is_campus_verified(user, now=None) -> bool:
    """Does ``user`` hold an active campus verification (not revoked AND within the TTL)?

    Time-based, never sweeper-based: a missed tick cannot extend anyone's access. A lapsed user gets
    exactly what a user who left campus gets: no accepting or being accepted, the lower bounty cap,
    and existing CLAIMED bounties untouched.
    """
    return bool(getattr(user, "is_authenticated", False)) and campus.is_campus_verified(user, now)


def open_bounty_cap(user, now=None) -> int:
    """How many live OPEN bounties ``user`` may have: the campus cap if campus-verified, else the
    email-verified cap. Read it under the user row lock (see services.create_bounty)."""
    if is_campus_verified(user, now):
        return settings.OPEN_BOUNTY_CAP_CAMPUS_VERIFIED
    return settings.OPEN_BOUNTY_CAP_EMAIL_VERIFIED


def can_view_contact(viewer, subject, bounty) -> bool:
    """May ``viewer`` see ``subject``'s contact (email, and the profile's contact handle)?

    True only when the bounty has an ACCEPTED claim and ``viewer`` and ``subject`` are its two
    different parties: the poster and the accepted claimant, in either direction. Nobody else,
    including moderators (moderator access to contact is a separate, audited action), and nobody
    before acceptance. Contact is the person's *profile*, not something typed at pitch time.

    Not re-checked against current verification: contact already exchanged stays visible after a
    later revocation, matching "existing CLAIMED bounties and contact already shown are left alone".
    """
    if not getattr(viewer, "is_authenticated", False) or viewer.id == subject.id:
        return False
    accepted = bounty.accepted_claim
    if accepted is None or accepted.status != ClaimStatus.ACCEPTED:
        return False
    parties = {bounty.poster_id, accepted.claimant_id}
    return viewer.id in parties and subject.id in parties


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

    Seeing the list is not seeing contact: that stays behind ``can_view_contact``.
    """
    return can_view_hidden_bounty(user, bounty)
