import logging
from django.conf import settings
from django.core.mail import send_mail

logger = logging.getLogger(__name__)


def absolute_url(path: str) -> str:
    """Absolute link for emails. The host comes from settings.SITE_URL, never from a literal."""
    return settings.SITE_URL.rstrip("/") + path


def send_claim_received_notification(claim):
    """Notify the bounty poster that a student has submitted a new claim."""
    poster = claim.bounty.poster
    if not poster.email:
        return
    
    subject = f"[Bounty Board] New claim on '{claim.bounty.title}'"
    message = (
        f"Hi {poster.username},\n\n"
        f"Student '{claim.claimant.username}' just claimed your bounty '{claim.bounty.title}' for ₹{claim.bounty.reward}.\n\n"
        f"Their pitch:\n"
        f"\"{claim.message}\"\n\n"
        f"Log in to review their claim and accept or reject it:\n"
        f"{absolute_url(claim.bounty.get_absolute_url())}\n\n"
        f"— Bounty Board Campus Marketplace"
    )
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[poster.email],
            fail_silently=True,
        )
    except Exception as exc:
        logger.warning("Failed sending claim received email: %s", exc)


def send_claim_accepted_notification(claim):
    """Notify the claimant that their claim was accepted by the poster."""
    claimant = claim.claimant
    if not claimant.email:
        return

    poster = claim.bounty.poster
    subject = f"[Bounty Board] 🎉 Your claim on '{claim.bounty.title}' was ACCEPTED!"
    message = (
        f"Congratulations {claimant.username}!\n\n"
        f"Your claim on '{claim.bounty.title}' has been accepted by {poster.username}.\n\n"
        f"Agreed Reward: ₹{claim.bounty.reward}\n"
        f"Deadline: {claim.bounty.deadline.strftime('%Y-%m-%d %H:%M')}\n\n"
        f"Poster Contact Email: {poster.email}\n"
        + (f"Poster Contact Handle: {poster.contact_handle}\n" if poster.contact_handle else "")
        + f"\nNEXT STEPS:\n"
        f"1. Reach out to {poster.username} directly using the contact details above.\n"
        f"2. Complete the deliverable before the deadline.\n"
        f"3. Receive payment of ₹{claim.bounty.reward} directly offline via UPI or Cash.\n\n"
        f"— Bounty Board Campus Marketplace"
    )
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[claimant.email],
            fail_silently=True,
        )
    except Exception as exc:
        logger.warning("Failed sending claim accepted email: %s", exc)


def send_claim_rejected_notification(claim):
    """Notify the claimant that their claim was rejected."""
    claimant = claim.claimant
    if not claimant.email:
        return

    subject = f"[Bounty Board] Update on '{claim.bounty.title}'"
    message = (
        f"Hi {claimant.username},\n\n"
        f"The poster has closed claims on '{claim.bounty.title}' or accepted another peer's proposal.\n\n"
        f"Check out other live bounties on the campus board:\n"
        f"{absolute_url('/')}\n\n"
        f"— Bounty Board Campus Marketplace"
    )
    try:
        send_mail(
            subject=subject,
            message=message,
            from_email=settings.DEFAULT_FROM_EMAIL,
            recipient_list=[claimant.email],
            fail_silently=True,
        )
    except Exception as exc:
        logger.warning("Failed sending claim rejected email: %s", exc)
