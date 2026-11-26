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


def renewal_reminder(n):
    """Rendered at send time from the user's current state, so the advice matches their situation."""
    from django.urls import reverse

    from apps.accounts import campus
    from apps.accounts.expiry import reminder_verification_pk

    user = n.recipient
    # Queued earlier, sent now: by then they may have renewed, lapsed, been revoked or deleted.
    verification = reminder_verification_pk(n.idempotency_key)
    if not campus.active_verifications().filter(pk=verification, user=user).exists():
        raise Obsolete("that verification is no longer about to lapse")
    if not user.is_active or user.email_verified_at is None:
        raise Obsolete("the account can no longer renew")
    subject = "[Bounty Board] Your campus verification expires soon"
    if campus.campus_email_allowed(user.email):
        action = (
            "Renew it in a minute: open the link, sign in, and we'll send a confirmation link to your "
            f"campus address.\n{absolute_url(reverse('accounts:renew'))}\n"
        )
    else:
        action = (
            "To renew, visit a verification desk with your LPU ID card once it has lapsed (your "
            "dashboard then shows the code to bring), or ask a moderator how to verify with a campus "
            "email address.\n"
        )
    body = (
        f"Hi {user.username},\n\n"
        "Your campus verification is about to lapse. After that you won't be able to accept a claim or "
        "be accepted, and your open-bounty limit drops. Bounties already accepted are not affected.\n\n"
        f"{action}\n— Bounty Board Campus Marketplace"
    )
    return user.email, subject, body


# kind -> callable(notification) -> (to_address, subject, body)
RENDERERS = {"CLAIM_ACCEPTED": claim_accepted, "RENEWAL_REMINDER": renewal_reminder}


def render(n):
    """Return (to, subject, body) for a notification, or raise KeyError if its kind is never emailed."""
    return RENDERERS[n.kind](n)


