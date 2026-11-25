"""Email bodies for outbox rows. Rendered at send time from the current rows, never stored.

Only kinds listed in ``RENDERERS`` are ever emailed; everything else is in-app only (the email
budget is 300/day, and verification and password-reset mail comes first).
"""

from django.utils import timezone

from .models import ClaimStatus
from .notifications import absolute_url


class Obsolete(Exception):
    """The event this row reports no longer holds, so the email must not go out."""


def claim_accepted(n):
    claim = n.claim
    # Read at send time: a retried email must not hand over contact after the claim was cancelled.
    if claim is None or claim.status != ClaimStatus.ACCEPTED:
        raise Obsolete("the claim is no longer accepted")
    claimant, poster, bounty = claim.claimant, claim.bounty.poster, claim.bounty
    title = " ".join(bounty.title.split())  # a line break in a header makes Django refuse to send
    subject = f"[Bounty Board] Your claim on '{title}' was accepted"
    body = (
        f"Hi {claimant.username},\n\n"
        f"Your claim on '{bounty.title}' was accepted by {poster.username}.\n\n"
        f"Agreed reward: ₹{bounty.reward}\n"
        f"Deadline: {timezone.localtime(bounty.deadline).strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Poster contact email: {poster.email}\n"
        + (f"Poster contact handle: {poster.contact_handle}\n" if poster.contact_handle else "")
        + "\nNext steps:\n"
        f"1. Reach out to {poster.username} using the details above.\n"
        "2. Complete the work before the deadline.\n"
        f"3. Payment of ₹{bounty.reward} is settled directly and offline (UPI or cash).\n\n"
        f"{absolute_url(bounty.get_absolute_url())}\n\n"
        "— Bounty Board Campus Marketplace"
    )
    return claimant.email, subject, body


# kind -> callable(notification) -> (to_address, subject, body)
RENDERERS = {"CLAIM_ACCEPTED": claim_accepted}


def render(n):
    """Return (to, subject, body) for a notification, or raise KeyError if its kind is never emailed."""
    return RENDERERS[n.kind](n)


