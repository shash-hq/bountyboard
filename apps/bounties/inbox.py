"""Writing and reading in-app notifications. The only module that touches `Notification` rows."""
from django.db import transaction
from django.utils import timezone

from .models import Notification

RECENT_LIMIT = 20


def notify(recipient, kind, message, idempotency_key, bounty=None):
    """Create the notification unless one with this key exists. Returns (notification, created).

    Must be called inside the transaction of the change it reports, so the row commits or rolls
    back with it (I7). `get_or_create` runs the insert in a savepoint, so losing the unique-key
    race does not poison the caller's transaction.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("notify() must be called inside transaction.atomic()")
    return Notification.objects.get_or_create(
        idempotency_key=idempotency_key,
        defaults={"recipient": recipient, "kind": kind, "message": message[:300], "bounty": bounty},
    )


def recent_for(user):
    """The user's newest notifications. Scoped to the recipient here, so no caller can widen it."""
    return list(Notification.objects.filter(recipient=user).select_related("bounty")[:RECENT_LIMIT])


def unread_count(user):
    return Notification.objects.filter(recipient=user, read_at__isnull=True).count()


def mark_all_read(user, now=None):
    return Notification.objects.filter(recipient=user, read_at__isnull=True).update(
        read_at=now or timezone.now()
    )
