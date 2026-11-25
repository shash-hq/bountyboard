"""Closing lapsed campus verifications, and (later in this module) renewal reminders.

Expiry is enforced by the policy, not by this sweeper: ``campus.active_verifications`` requires
``verified_at`` within the TTL, so a sleeping instance or a missed tick cannot extend anyone's
access. The sweeper only *records* the lapse (revoke_reason EXPIRED, with an AuditEvent) so that the
"one unrevoked row per user" index stays true and a renewal can insert a fresh row. It sends no
notification to counterparties: a lapse is not a finding against anyone, and existing CLAIMED
bounties carry on exactly as they do after LEFT_CAMPUS.
"""
import logging
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .models import AuditEvent, CampusVerification, RevokeReason, User

logger = logging.getLogger(__name__)


REMINDER_KEY = "renewal-reminder:"


def reminder_verification_pk(idempotency_key: str) -> int:
    """Which verification a reminder row was queued for."""
    return int(idempotency_key.removeprefix(REMINDER_KEY))


def _cutoff(now):
    return now - timedelta(days=settings.VERIFICATION_TTL_DAYS)


def expire_lapsed(now=None, limit=100) -> int:
    """Close up to ``limit`` lapsed, still-unrevoked rows (oldest first). One transaction per row,
    holding that user's row lock, re-checking under it. Returns how many were closed."""
    now = now or timezone.now()
    cutoff = _cutoff(now)
    ids = list(
        CampusVerification.objects.filter(revoked_at__isnull=True, verified_at__lte=cutoff)
        .order_by("verified_at", "pk").values_list("pk", "user_id")[:limit]
    )
    closed = 0
    for pk, user_id in ids:
        try:
            with transaction.atomic():
                user = User.objects.select_for_update(no_key=True).get(pk=user_id)
                row = CampusVerification.objects.filter(
                    pk=pk, revoked_at__isnull=True, verified_at__lte=cutoff
                ).first()
                if row is None:  # renewed or revoked since we looked
                    continue
                row.revoked_at, row.revoke_reason = now, RevokeReason.EXPIRED
                row.save(update_fields=["revoked_at", "revoke_reason"])
                AuditEvent.objects.create(
                    actor=None, subject=user, action="REVOKED", reason=RevokeReason.EXPIRED,
                    verification=row, created_at=now,
                )
                closed += 1
        except Exception:  # noqa: BLE001 - keep going; the next tick retries this row
            logger.exception("Could not expire verification %s", pk)
    return closed


def remind_expiring(now=None, limit=None) -> int:
    """Queue one renewal reminder per verification that lapses within RENEWAL_REMINDER_DAYS.

    Oldest first, capped at RENEWAL_REMINDERS_PER_DAY per rolling 24 h (counting the Notification rows
    already created), and at most once per row (``renewal_reminder_sent_at`` is set in the same
    transaction as the outbox row, under the user's row lock). The email itself is delivered by the
    outbox, which has its own daily budget on top.
    """
    from apps.bounties import inbox
    from apps.bounties.models import Notification, NotificationKind

    now = now or timezone.now()
    cutoff = _cutoff(now)
    window_end = cutoff + timedelta(days=settings.RENEWAL_REMINDER_DAYS)
    already = Notification.objects.filter(
        kind=NotificationKind.RENEWAL_REMINDER, created_at__gt=now - timedelta(hours=24)
    ).count()
    budget = min(limit or settings.RENEWAL_REMINDERS_PER_DAY, settings.RENEWAL_REMINDERS_PER_DAY - already)
    if budget <= 0:
        return 0
    due = list(
        CampusVerification.objects.filter(
            revoked_at__isnull=True, renewal_reminder_sent_at__isnull=True,
            verified_at__gt=cutoff, verified_at__lte=window_end,
            # Here and not only in the loop: accounts that are always skipped would fill every batch.
            user__is_active=True, user__email_verified_at__isnull=False,
        ).order_by("verified_at", "pk").values_list("pk", "user_id")[:budget]
    )
    sent = 0
    for pk, user_id in due:
        try:
            with transaction.atomic():
                user = User.objects.select_for_update(no_key=True).get(pk=user_id)
                row = CampusVerification.objects.filter(
                    pk=pk, revoked_at__isnull=True, renewal_reminder_sent_at__isnull=True, verified_at__gt=cutoff,
                ).first()
                if row is None or not user.is_active or user.email_verified_at is None:
                    continue
                row.renewal_reminder_sent_at = now
                row.save(update_fields=["renewal_reminder_sent_at"])
                lapses = timezone.localtime(row.verified_at + timedelta(days=settings.VERIFICATION_TTL_DAYS))
                inbox.notify(
                    user, NotificationKind.RENEWAL_REMINDER,
                    f"Your campus verification expires on {lapses:%b %d, %Y}. Renew it from your dashboard.",
                    f"{REMINDER_KEY}{row.pk}", email=True, now=now,
                )
                sent += 1
        except Exception:  # noqa: BLE001
            logger.exception("Could not queue a renewal reminder for verification %s", pk)
    return sent


def sweep_verifications(now=None) -> dict:
    """The tick's job for verifications: close what lapsed, then remind those about to."""
    return {"expired": expire_lapsed(now), "reminded": remind_expiring(now)}
