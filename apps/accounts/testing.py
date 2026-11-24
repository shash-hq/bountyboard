"""Test helpers. Production code never imports this module."""
from django.contrib.auth import get_user_model
from django.utils import timezone


def create_verified_user(*args, **kwargs):
    """``User.objects.create_user`` with a confirmed email.

    Plain ``create_user`` yields an UNVERIFIED account (that is the point: signup is fail-closed),
    which can neither log in nor stay logged in. Tests that are about something else use this.
    """
    kwargs.setdefault("email_verified_at", timezone.now())
    return get_user_model().objects.create_user(*args, **kwargs)


def campus_verify(user, **kwargs):
    """Give ``user`` an active CAMPUS_EMAIL verification, without going through email confirmation.

    For tests about something else (caps, accepting claims) that need a campus-verified user. Tests
    of verification itself use the real paths (confirm_email, verify_in_person).
    """
    from apps.accounts.models import CampusVerification

    kwargs.setdefault("verified_email", f"{user.username}@lpu.co.in")
    kwargs.setdefault("verified_at", timezone.now())
    CampusVerification.objects.create(user=user, method="CAMPUS_EMAIL", **kwargs)
    return user
