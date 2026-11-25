"""Delivery of outbox rows (Notification.email_status=PENDING).

Two entry points, one code path: a best-effort attempt right after the originating transaction
commits (latency), and ``drain`` from ``/internal/tick`` (retries, and anything the first attempt
missed because the instance slept or crashed).

Concurrency: each row is delivered in its own short transaction with
``SELECT ... FOR UPDATE SKIP LOCKED``, so the on-commit attempt and any number of ticks never send
the same row twice at once and never block each other. Delivery is at-least-once: if the process dies
after SMTP accepts a message but before our commit, the row is still PENDING and is sent again.

Budget: SENT rows in the last 24 h are counted against ``OUTBOX_EMAIL_DAILY_CAP``, which is kept well
under the provider's 300/day so account-verification and password-reset mail always has room.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from . import emails
from .models import EmailStatus, Notification

logger = logging.getLogger(__name__)

BACKOFF_BASE = timedelta(minutes=1)
BACKOFF_MAX = timedelta(hours=6)


def backoff(attempts: int) -> timedelta:
    """Delay before attempt number ``attempts + 1``: 1 min, 2, 4, ... capped at 6 h."""
    return min(BACKOFF_BASE * (2 ** max(attempts - 1, 0)), BACKOFF_MAX)


def sent_last_24h(now) -> int:
    return Notification.objects.filter(email_status=EmailStatus.SENT, delivered_at__gt=now - timedelta(hours=24)).count()


def _due(now):
    return Notification.objects.filter(email_status=EmailStatus.PENDING, next_attempt_at__lte=now)


def deliver_one(pk=None, now=None) -> str:
    """Deliver one due row (the given one, or the next due one). Returns what happened:
    ``"sent"``, ``"retry"``, ``"dead"``, ``"budget"`` (cap reached, row left PENDING) or ``"none"``."""
    now = now or timezone.now()
    with transaction.atomic():
        qs = _due(now).select_related("recipient", "bounty").order_by("next_attempt_at", "pk")
        if pk is not None:
            qs = qs.filter(pk=pk)
        n = qs.select_for_update(skip_locked=True, of=("self",)).first()
        if n is None:
            return "none"
        if sent_last_24h(now) >= settings.OUTBOX_EMAIL_DAILY_CAP:
            return "budget"
        try:
            to, subject, body = emails.render(n)
            if not to:
                raise ValueError("recipient has no email address")
            EmailMessage(subject, body, settings.DEFAULT_FROM_EMAIL, [to]).send(fail_silently=False)
        except Exception as exc:  # noqa: BLE001 - any failure is retried, then given up on
            n.attempts += 1
            n.last_error = f"{type(exc).__name__}: {exc}"[:200]
            if n.attempts >= settings.OUTBOX_MAX_ATTEMPTS:
                n.email_status, n.next_attempt_at = EmailStatus.DEAD, None
                outcome = "dead"
                logger.error("Notification %s gave up after %s attempts: %s", n.pk, n.attempts, n.last_error)
            else:
                n.next_attempt_at = now + backoff(n.attempts)
                outcome = "retry"
            n.save(update_fields=["attempts", "last_error", "email_status", "next_attempt_at"])
            return outcome
        n.attempts += 1
        n.email_status, n.next_attempt_at, n.delivered_at, n.last_error = EmailStatus.SENT, None, now, ""
        n.save(update_fields=["attempts", "email_status", "next_attempt_at", "delivered_at", "last_error"])
        return "sent"


def deliver_one_best_effort(pk) -> None:
    """The on-commit attempt: never raises into the request that triggered it."""
    try:
        deliver_one(pk)
    except Exception:  # noqa: BLE001
        logger.exception("on-commit delivery of notification %s failed; the tick will retry it", pk)


def drain(now=None, limit=None, max_seconds=None) -> dict:
    """Deliver due rows until none are left, ``limit`` rows were handled or ``max_seconds`` elapsed
    (the tick runs inside a web request on a 0.1-CPU instance, so it must stay short)."""
    started = timezone.now()
    now = now or started
    limit = limit or settings.OUTBOX_DRAIN_LIMIT
    counts = {"sent": 0, "retry": 0, "dead": 0}
    for _ in range(limit):
        if max_seconds is not None and (timezone.now() - started).total_seconds() > max_seconds:
            break
        outcome = deliver_one(now=now)
        if outcome in ("none", "budget"):
            counts["stopped"] = outcome
            break
        counts[outcome] += 1
    return counts
