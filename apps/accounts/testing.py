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
