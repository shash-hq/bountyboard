"""Writing and reading in-app notifications. The only module that touches `Notification` rows."""
from django.db import transaction
from django.utils import timezone

from .models import EmailStatus, Notification

RECENT_LIMIT = 20


def notify(recipient, kind, message, idempotency_key, bounty=None, claim=None, email=False, now=None):
    """Create the notification unless one with this key exists. Returns (notification, created).

    ``email=True`` queues it for the outbox (PENDING, due now) and schedules one delivery attempt
    for after the surrounding transaction commits. Only call that for the few kinds worth an email.

    Must be called inside the transaction of the change it reports, so the row commits or rolls
    back with it (I7). `get_or_create` runs the insert in a savepoint, so losing the unique-key
    race does not poison the caller's transaction.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("notify() must be called inside transaction.atomic()")
    now = now or timezone.now()
    defaults = {"recipient": recipient, "kind": kind, "message": message[:300], "bounty": bounty, "claim": claim,
                "created_at": now}
    if email:
        defaults.update(email_status=EmailStatus.PENDING, next_attempt_at=now)
    notification, created = Notification.objects.get_or_create(idempotency_key=idempotency_key, defaults=defaults)
    if created and email:
        from . import outbox

        transaction.on_commit(lambda pk=notification.pk: outbox.deliver_one_best_effort(pk))
    return notification, created


def recent_for(user):
    """The user's newest notifications. Scoped to the recipient here, so no caller can widen it."""
    return list(Notification.objects.filter(recipient=user).select_related("bounty")[:RECENT_LIMIT])


def unread_count(user):
    return Notification.objects.filter(recipient=user, read_at__isnull=True).count()


def mark_all_read(user, now=None):
    return Notification.objects.filter(recipient=user, read_at__isnull=True).update(
        read_at=now or timezone.now()
    )
