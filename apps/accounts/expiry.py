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
                user = User.objects.select_for_update().get(pk=user_id)
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


def sweep_verifications(now=None) -> dict:
    return {"expired": expire_lapsed(now)}
