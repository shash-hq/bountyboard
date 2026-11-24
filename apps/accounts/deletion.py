"""Account deletion: anonymize in place.

The user row is kept but scrubbed and deactivated, never deleted. Why not ``DELETE``:
- ``Bounty.poster`` and ``Claim.claimant`` are CASCADE, so a hard delete would erase a counterparty's
  finished bounties and claims, and for an accepted claimant it would null ``accepted_claim`` on a
  CLAIMED bounty and violate the I2 check constraint (the delete would crash).
- Keeping the row keeps the ``StudentIdentity`` binding attached, so deleting an account can never
  free a registration number. A student revoked as invalid or for a violation therefore cannot
  delete and re-verify the same card; a new account with that number collides and is flagged for
  review like any duplicate. The revocation history stays too, so the ban state survives.

What is removed: username, email, names, contact handle, password, group/permission/staff grants, the verification
email token, the encrypted card name, the campus-email snapshot, and free-text claim contact. What
stays: ids, bounties, claims, reports, audit rows, the HMAC binding and revocation rows (no personal
data: a keyed hash, last 4 digits, reasons). Bounty and claim *text* the user wrote stays too, shown
under the placeholder username; scrubbing user-authored content is a separate decision.

Lock order (CLAUDE.local.md): bounty -> claims -> user. The cleanup goes through the bounties
services, each in its own transaction (which take bounty then claims). Only after those commit do we
lock the user row, so we never hold the user lock while a service takes a bounty lock.
"""
import secrets

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.bounties import services as bounty_services
from apps.bounties.exceptions import InvalidTransition
from apps.bounties.models import Bounty, BountyStatus, Claim, ClaimStatus

from .errors import DeletionBlocked, InvalidState
from .models import AuditEvent, CampusVerification, RevokeReason, User, VerificationMethod


def _in_flight(user_id):
    """Bounties that cannot be abandoned: CLAIMED, as poster or as the accepted claimant."""
    return Bounty.objects.filter(status=BountyStatus.CLAIMED).filter(
        Q(poster_id=user_id) | Q(accepted_claim__claimant_id=user_id)
    )


def _blocked() -> DeletionBlocked:
    return DeletionBlocked(
        "You can't delete your account while a task you posted or accepted is in progress. "
        "Finish it first, then try again."
    )


def delete_account(user_id, now=None) -> None:
    now = now or timezone.now()
    user = User.objects.filter(pk=user_id).first()
    if user is None or user.deleted_at is not None:
        raise InvalidState("This account is already deleted.")

    # 1. Refuse before any side effect, so a blocked user's open bounties are left alone.
    if _in_flight(user_id).exists():
        raise _blocked()

    # 2. Close out what the user can walk away from, through the services (state machine, outbox).
    for bounty in Bounty.objects.filter(poster_id=user_id, status=BountyStatus.OPEN):
        try:
            bounty_services.cancel_bounty(bounty.pk, user)
        except InvalidTransition:
            pass  # it moved on since we listed it; the re-check below decides
    for claim in Claim.objects.filter(claimant_id=user_id, status=ClaimStatus.PENDING):
        try:
            bounty_services.withdraw_claim(claim.pk, user)
        except InvalidTransition:
            pass

    # 3. Under the user lock: re-check (a claim may have been accepted, or a bounty created, since
    # step 1) and anonymize, with one audit row, atomically.
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user_id)
        if locked.deleted_at is not None:
            raise InvalidState("This account is already deleted.")
        if (
            _in_flight(user_id).exists()
            or Bounty.objects.filter(poster_id=user_id, status=BountyStatus.OPEN).exists()
            or Claim.objects.filter(claimant_id=user_id, status=ClaimStatus.PENDING).exists()
        ):
            raise _blocked()
        _anonymize(locked, now)


def _anonymize(user, now) -> None:
    # Close the verification first: the IN_PERSON check only lets a CLOSED row lose its card name.
    active = CampusVerification.objects.filter(user=user, revoked_at__isnull=True)
    verification = active.first()
    active.update(revoked_at=now, revoke_reason=RevokeReason.ACCOUNT_DELETED)
    mine = CampusVerification.objects.filter(user=user)
    mine.filter(method=VerificationMethod.IN_PERSON).update(card_name_enc=None)
    mine.filter(method=VerificationMethod.CAMPUS_EMAIL).update(verified_email="")
    Claim.objects.filter(claimant=user).update(contact="")  # free-text phone/handle

    # Unique, unguessable placeholders: a fixed "deleted-<id>" could be pre-registered by someone
    # else and make this save violate the case-insensitive unique constraints.
    label = f"deleted-{user.pk}-{secrets.token_hex(4)}"
    user.username, user.email = label, f"{label}@deleted.invalid"
    user.first_name = user.last_name = user.contact_handle = ""
    user.set_unusable_password()
    user.is_active = user.is_staff = user.is_superuser = False
    user.email_token_hash = user.email_token_expires_at = None
    user.staff_since = user.staff_source = user.staff_granted_by = None
    user.deleted_at = now
    user.save()
    user.groups.clear()
    user.user_permissions.clear()
    AuditEvent.objects.create(
        actor=user, subject=user, action="ACCOUNT_DELETED", verification=verification, created_at=now
    )
