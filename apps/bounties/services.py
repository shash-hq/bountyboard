from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .exceptions import DomainError, InvalidTransition, LimitReached, NotPermitted  # noqa: F401 (re-exported)
from .models import Bounty, BountyStatus, Claim, ClaimStatus, Report
from .policies import can_moderate, is_campus_verified, open_bounty_cap
from .notifications import (
    send_claim_accepted_notification,
    send_claim_rejected_notification,
)


def _lock_bounty(bounty_id: int) -> Bounty:
    """Lock the bounty row (FIRST in the fixed lock order: bounty -> claims -> user).

    Every transition that touches a bounty or one of its claims takes this lock before
    anything else and re-reads state afterwards, so concurrent transitions on the same
    bounty are serialized and each one validates against the committed result of the last.
    """
    try:
        return Bounty.objects.select_for_update().get(id=bounty_id)
    except Bounty.DoesNotExist:
        raise InvalidTransition("Bounty does not exist.")


def _lock_claim(claim_id: int) -> Claim:
    """Lock a claim row. Callers must already hold its bounty's lock."""
    try:
        return Claim.objects.select_for_update().get(id=claim_id)
    except Claim.DoesNotExist:
        raise InvalidTransition("Claim does not exist.")


def _bounty_id_of_claim(claim_id: int) -> int:
    """Unlocked read of claim.bounty_id. Safe because a claim never changes bounty;
    it only tells us WHICH bounty to lock first (the claim is re-read under the lock)."""
    bounty_id = Claim.objects.filter(id=claim_id).values_list("bounty_id", flat=True).first()
    if bounty_id is None:
        raise InvalidTransition("Claim does not exist.")
    return bounty_id


EDITABLE_FIELDS = ("title", "description", "category", "reward", "deadline")


def count_live_open_bounties(user) -> int:
    """The user's OPEN bounties, hidden ones included (invariant I4): hiding a listing must not be a way
    to post past the cap."""
    return Bounty.objects.filter(poster=user, status=BountyStatus.OPEN).count()


def open_limit_message(user, cap: int) -> str:
    noun = "bounty" if cap == 1 else "bounties"
    msg = f"You already have {cap} open {noun}. Complete or cancel one before posting more."
    if not is_campus_verified(user):
        msg += f" Campus-verified students can have up to {settings.OPEN_BOUNTY_CAP_CAMPUS_VERIFIED} open at once."
    return msg


def create_bounty(poster, **fields) -> Bounty:
    """Create a bounty, serializing the open-bounty cap (invariant I4).

    The cap depends on the poster's tier (1 email-verified, 5 campus-verified) and the count must
    include hidden bounties. Counting and then inserting is a write-skew race: two concurrent creates
    can both count 4 and both insert. Locking the poster's USER row first makes concurrent creates by
    the same poster take turns, so each count sees the previous create's committed row. The same row
    is locked by revoke_verification, so the tier cannot change between reading the cap and the insert.
    (No bounty exists yet, so there is no bounty lock; the user lock is last in bounty -> claims -> user.)
    """
    User = get_user_model()
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=poster.pk)
        cap = open_bounty_cap(locked)
        if count_live_open_bounties(locked) >= cap:
            raise LimitReached(open_limit_message(locked, cap))
        bounty = Bounty(poster=poster, **fields)
        bounty.save()  # full_clean() validates the fields; the cap is enforced above, not in clean()
    return bounty


def update_bounty_details(bounty_id: int, user, **fields) -> Bounty:
    """Poster edits an OPEN bounty's details.

    Locks the bounty, re-checks that it is still OPEN and not expired AFTER the lock, and
    writes ONLY the editable columns. The old view saved a full row from an unlocked read, so
    a concurrent accept/cancel/hide was overwritten with stale status/accepted_claim/is_hidden.
    """
    unknown = set(fields) - set(EDITABLE_FIELDS)
    if unknown:
        raise TypeError(f"Not editable: {sorted(unknown)}")
    with transaction.atomic():
        bounty = _lock_bounty(bounty_id)

        if user != bounty.poster:
            raise NotPermitted("You do not have permission to edit this bounty.")
        if bounty.status != BountyStatus.OPEN or bounty.is_expired:
            raise InvalidTransition("Only active OPEN bounties can be edited.")

        for name, value in fields.items():
            setattr(bounty, name, value)
        bounty.full_clean()
        bounty.save(update_fields=[*fields, "updated_at"])
    return bounty


def create_claim(bounty_id: int, claimant, message: str, contact: str) -> Claim:
    """Submit a claim. Locks the bounty so a claim cannot land on a bounty that was
    accepted/cancelled concurrently, and so a double-submit is detected, not a 500."""
    with transaction.atomic():
        bounty = _lock_bounty(bounty_id)

        if bounty.poster_id == claimant.id:
            raise InvalidTransition("You cannot claim your own bounty.")
        if bounty.status != BountyStatus.OPEN or bounty.is_expired or bounty.is_hidden:
            raise InvalidTransition("This bounty is no longer accepting claims.")
        if Claim.objects.filter(bounty=bounty, claimant=claimant).exists():
            raise InvalidTransition("You have already submitted a claim on this bounty.")

        claim = Claim(bounty=bounty, claimant=claimant, message=message, contact=contact)
        claim.save()
    return claim


def _require_both_campus_verified(poster_id: int, claimant_id: int) -> None:
    """Invariant I9: contact is exchanged only between two campus-verified users.

    Takes both USER row locks in ascending id order (the last step of bounty -> claims -> user) and
    checks verification on the locked rows. revoke_verification, delete_account and create_bounty
    lock the same rows, so a revoke either commits before this check (and the accept fails) or waits
    until this transaction commits (and the accept had already seen a valid verification). Never:
    contact released to someone whose verification was revoked before the accept committed.
    """
    User = get_user_model()
    locked = {u.pk: u for u in User.objects.select_for_update().filter(pk__in={poster_id, claimant_id}).order_by("pk")}
    poster, claimant = locked.get(poster_id), locked.get(claimant_id)
    now = timezone.now()
    if poster is None or not poster.is_active or not is_campus_verified(poster, now):
        raise NotPermitted(
            "Verify your campus identity before accepting a claim: it protects both of you before "
            "contact details are shared."
        )
    if claimant is None or not claimant.is_active or not is_campus_verified(claimant, now):
        raise NotPermitted(
            "This claimant hasn't verified their campus identity yet, so you can't accept them. "
            "Pick another claim, or ask them to verify first."
        )


def accept_claim(claim_id: int, user) -> Claim:
    """
    Atomically accepts one claim and rejects all other pending claims on the bounty.
    Lock order: bounty, then claims, then users. State is re-validated after the locks are held.
    """
    with transaction.atomic():
        bounty = _lock_bounty(_bounty_id_of_claim(claim_id))
        claim = _lock_claim(claim_id)

        # Permission check: Only the poster can accept claims
        if user != bounty.poster:
            raise NotPermitted("Only the bounty poster can accept claims.")

        # Status checks, on the freshly locked rows
        if bounty.status != BountyStatus.OPEN:
            raise InvalidTransition(f"Cannot accept claims on a bounty with status '{bounty.status}'.")

        if bounty.is_expired:
            raise InvalidTransition("Cannot accept claims on an expired bounty.")

        if claim.status != ClaimStatus.PENDING:
            raise InvalidTransition(f"Cannot accept a claim with status '{claim.status}'.")

        # Lock the other PENDING claims now (in id order), BEFORE the user rows: claims come before
        # users in the fixed lock order.
        other_claims = list(
            Claim.objects.select_for_update()
            .filter(bounty=bounty, status=ClaimStatus.PENDING)
            .exclude(id=claim.id)
            .order_by("id")
        )

        # I9: both parties campus-verified, checked under the user locks.
        _require_both_campus_verified(bounty.poster_id, claim.claimant_id)

        # Update the accepted claim
        claim.status = ClaimStatus.ACCEPTED
        claim.save(update_fields=["status", "updated_at"])

        # Update bounty status and accepted_claim pointer
        bounty.status = BountyStatus.CLAIMED
        bounty.accepted_claim = claim
        bounty.save(update_fields=["status", "accepted_claim", "updated_at"])

        # Reject all other PENDING claims on this bounty
        Claim.objects.filter(id__in=[c.id for c in other_claims]).update(status=ClaimStatus.REJECTED)

    # Dispatch notifications after transaction commits
    transaction.on_commit(lambda: send_claim_accepted_notification(claim))
    for other in other_claims:
        transaction.on_commit(lambda c=other: send_claim_rejected_notification(c))

    return claim


def reject_claim(claim_id: int, user) -> Claim:
    """Poster explicitly rejects a pending claim."""
    with transaction.atomic():
        bounty = _lock_bounty(_bounty_id_of_claim(claim_id))
        claim = _lock_claim(claim_id)

        if user != bounty.poster:
            raise NotPermitted("Only the bounty poster can reject claims.")

        if claim.status != ClaimStatus.PENDING:
            raise InvalidTransition(f"Cannot reject a claim that is already '{claim.status}'.")

        claim.status = ClaimStatus.REJECTED
        claim.save(update_fields=["status", "updated_at"])

    send_claim_rejected_notification(claim)
    return claim


def withdraw_claim(claim_id: int, user) -> Claim:
    """Claimant withdraws their own pending claim."""
    with transaction.atomic():
        _lock_bounty(_bounty_id_of_claim(claim_id))
        claim = _lock_claim(claim_id)

        if user.id != claim.claimant_id:
            raise NotPermitted("You can only withdraw your own claims.")

        if claim.status != ClaimStatus.PENDING:
            raise InvalidTransition(f"Cannot withdraw a claim that is '{claim.status}'.")

        claim.status = ClaimStatus.WITHDRAWN
        claim.save(update_fields=["status", "updated_at"])
    return claim


def mark_bounty_done(bounty_id: int, user) -> Bounty:
    """Poster marks an in-progress claimed bounty as DONE."""
    with transaction.atomic():
        bounty = _lock_bounty(bounty_id)

        if user != bounty.poster:
            raise NotPermitted("Only the bounty poster can mark a bounty as done.")

        if bounty.status != BountyStatus.CLAIMED:
            raise InvalidTransition("Only claimed bounties can be marked done.")

        bounty.status = BountyStatus.DONE
        bounty.save(update_fields=["status", "updated_at"])
    return bounty


def cancel_bounty(bounty_id: int, user) -> Bounty:
    """Poster cancels an open bounty."""
    with transaction.atomic():
        bounty = _lock_bounty(bounty_id)

        if user != bounty.poster:
            raise NotPermitted("Only the bounty poster can cancel a bounty.")

        if bounty.status != BountyStatus.OPEN:
            raise InvalidTransition("Only OPEN bounties can be cancelled.")

        bounty.status = BountyStatus.CANCELLED
        bounty.save(update_fields=["status", "updated_at"])
        # Also reject any pending claims, in the same transaction, under the bounty lock
        pending_ids = list(
            Claim.objects.select_for_update()
            .filter(bounty=bounty, status=ClaimStatus.PENDING)
            .order_by("id")
            .values_list("id", flat=True)
        )
        Claim.objects.filter(id__in=pending_ids).update(status=ClaimStatus.REJECTED)
    return bounty


def hide_bounty(bounty_id: int, user, is_hidden: bool = True) -> Bounty:
    """Moderator toggles visibility of a bounty."""
    if not can_moderate(user):
        raise NotPermitted("You do not have permission to moderate bounties.")

    try:
        bounty = Bounty.objects.get(id=bounty_id)
    except Bounty.DoesNotExist:
        raise InvalidTransition("Bounty does not exist.")

    bounty.is_hidden = is_hidden
    bounty.save(update_fields=["is_hidden", "updated_at"])
    return bounty


def resolve_report(report_id: int, user, hide_bounty_flag: bool = False) -> Report:
    """Moderator resolves an abuse report, optionally hiding the associated bounty."""
    if not can_moderate(user):
        raise NotPermitted("You do not have permission to moderate reports.")

    try:
        report = Report.objects.select_related("bounty").get(id=report_id)
    except Report.DoesNotExist:
        raise InvalidTransition("Report does not exist.")

    report.resolved = True
    report.save(update_fields=["resolved"])

    if hide_bounty_flag:
        report.bounty.is_hidden = True
        report.bounty.save(update_fields=["is_hidden", "updated_at"])

    return report
