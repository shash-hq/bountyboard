import logging

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from .exceptions import DomainError, InvalidTransition, LimitReached, NotPermitted  # noqa: F401 (re-exported)
from .models import Bounty, BountyStatus, Claim, ClaimStatus, Report
from . import inbox
from .models import NotificationKind
from .policies import (
    can_cancel_after_counterparty_revocation,
    can_moderate,
    counterparty_of,
    is_campus_verified,
    open_bounty_cap,
)

logger = logging.getLogger(__name__)


def _lock_bounty(bounty_id: int) -> Bounty:
    """Lock the bounty row (FIRST in the fixed lock order: bounty -> claims -> user).

    FOR NO KEY UPDATE, here and on user rows: a revocation inserts notifications whose foreign keys
    point at the bounty and the other party, and those checks would deadlock against FOR UPDATE.

    Every transition that touches a bounty or one of its claims takes this lock before
    anything else and re-reads state afterwards, so concurrent transitions on the same
    bounty are serialized and each one validates against the committed result of the last.
    """
    try:
        return Bounty.objects.select_for_update(no_key=True).get(id=bounty_id)
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


def create_claim(bounty_id: int, claimant, message: str) -> Claim:
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

        claim = Claim(bounty=bounty, claimant=claimant, message=message)
        claim.save()
        # No claimant contact in the text: nothing is revealed before acceptance.
        inbox.notify(
            bounty.poster, NotificationKind.CLAIM_RECEIVED,
            f"{claimant.username} claimed '{bounty.title}'. Open the bounty to review the claim.",
            f"claim-received:{claim.pk}", bounty=bounty, claim=claim,
        )
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
    locked = {u.pk: u for u in User.objects.select_for_update(no_key=True).filter(pk__in={poster_id, claimant_id}).order_by("pk")}
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
        Claim.objects.filter(id__in=[c.id for c in other_claims]).update(
            status=ClaimStatus.REJECTED, updated_at=timezone.now()
        )

        # I7: the outbox rows commit with the transition. Acceptance is the one kind that is emailed
        # (the claimant needs the poster's contact, and email is scarce); the rest are in-app only.
        inbox.notify(
            claim.claimant, NotificationKind.CLAIM_ACCEPTED,
            f"{bounty.poster.username} accepted your claim on '{bounty.title}'. Their contact details are on the bounty page.",
            f"claim-accepted:{claim.pk}", bounty=bounty, claim=claim, email=True,
        )
        for other in other_claims:
            inbox.notify(
                other.claimant, NotificationKind.CLAIM_REJECTED,
                f"Your claim on '{bounty.title}' was not chosen.",
                f"claim-rejected:{other.pk}", bounty=bounty, claim=other,
            )

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
        inbox.notify(
            claim.claimant, NotificationKind.CLAIM_REJECTED,
            f"Your claim on '{bounty.title}' was not chosen.",
            f"claim-rejected:{claim.pk}", bounty=bounty, claim=claim,
        )
    return claim


def withdraw_claim(claim_id: int, user) -> Claim:
    """Claimant withdraws their own pending claim."""
    with transaction.atomic():
        bounty = _lock_bounty(_bounty_id_of_claim(claim_id))
        claim = _lock_claim(claim_id)

        if user.id != claim.claimant_id:
            raise NotPermitted("You can only withdraw your own claims.")

        if claim.status != ClaimStatus.PENDING:
            raise InvalidTransition(f"Cannot withdraw a claim that is '{claim.status}'.")

        claim.status = ClaimStatus.WITHDRAWN
        claim.save(update_fields=["status", "updated_at"])
        inbox.notify(
            bounty.poster, NotificationKind.CLAIM_WITHDRAWN,
            f"{claim.claimant.username} withdrew their claim on '{bounty.title}'.",
            f"claim-withdrawn:{claim.pk}", bounty=bounty, claim=claim,
        )
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
        accepted = bounty.accepted_claim
        if accepted is not None:
            inbox.notify(
                accepted.claimant, NotificationKind.BOUNTY_DONE,
                f"{bounty.poster.username} marked '{bounty.title}' as done. Settle payment offline.",
                f"bounty-done:{bounty.pk}", bounty=bounty, claim=accepted,
            )
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
        pending = list(
            Claim.objects.select_for_update(of=("self",)).select_related("claimant")
            .filter(bounty=bounty, status=ClaimStatus.PENDING)
            .order_by("id")
        )
        Claim.objects.filter(id__in=[c.id for c in pending]).update(status=ClaimStatus.REJECTED, updated_at=timezone.now())
        for c in pending:
            inbox.notify(
                c.claimant, NotificationKind.BOUNTY_CANCELLED,
                f"'{bounty.title}' was cancelled by its poster.",
                f"bounty-cancelled:{bounty.pk}:{c.pk}", bounty=bounty, claim=c,
            )
    return bounty


def cancel_after_counterparty_revocation(bounty_id: int, user) -> Bounty:
    """One party walks away from a CLAIMED bounty because the other's verification was revoked.

    CLAIMED -> CANCELLED, the accepted claim -> CANCELLED and ``accepted_claim`` -> NULL, which is
    what keeps I2 (a bounty is CLAIMED iff it points at an ACCEPTED claim). Lock order: bounty,
    the accepted claim, then both users in ascending id; the policy is evaluated on those locked
    rows, so a re-verification that commits first makes the cancel fail, and a revoke that commits
    first makes it succeed. The other party gets one in-app notification in the same transaction.
    """
    with transaction.atomic():
        bounty = _lock_bounty(bounty_id)
        if bounty.status != BountyStatus.CLAIMED or bounty.accepted_claim_id is None:
            raise InvalidTransition("Only a claimed bounty can be cancelled this way.")
        claim = _lock_claim(bounty.accepted_claim_id)
        if claim.status != ClaimStatus.ACCEPTED:
            raise InvalidTransition("This bounty has no accepted claim to cancel.")
        if user.id not in (bounty.poster_id, claim.claimant_id):
            raise NotPermitted("Only the two parties to this bounty can cancel it.")

        # Users last, ascending id. The returned rows are the locked, fresh ones.
        User = get_user_model()
        locked = {u.pk: u for u in User.objects.select_for_update(no_key=True).filter(
            pk__in={bounty.poster_id, claim.claimant_id}).order_by("pk")}
        bounty.accepted_claim = claim
        claim.claimant = locked[claim.claimant_id]
        bounty.poster = locked[bounty.poster_id]
        actor = locked[user.id]
        if not can_cancel_after_counterparty_revocation(actor, bounty):
            raise NotPermitted(
                "You can cancel only after the other person's campus verification was revoked "
                "for an invalid verification or a policy violation."
            )
        other = counterparty_of(actor, bounty)

        claim.status = ClaimStatus.CANCELLED
        claim.save(update_fields=["status", "updated_at"])
        bounty.status = BountyStatus.CANCELLED
        bounty.accepted_claim = None
        bounty.save(update_fields=["status", "accepted_claim", "updated_at"])
        inbox.notify(
            other, NotificationKind.COUNTERPARTY_CANCELLED,
            f"{actor.username} cancelled '{bounty.title}' after your campus verification was revoked.",
            idempotency_key=f"cancel-after-revocation:{bounty.pk}:{claim.pk}", bounty=bounty,
        )
    return bounty


def expire_bounty(bounty_id: int, now=None) -> bool:
    """Close one overdue OPEN bounty: OPEN -> EXPIRED, pending claims -> REJECTED, notify everyone.

    Decision 4: expiry is a real state, set here rather than inferred. Lock order is bounty, then
    its claims (no user rows are needed). State is re-read under the lock, so a bounty that was
    accepted, cancelled or extended since the sweeper looked at it is left alone. Returns whether
    it expired the bounty. Safe to run repeatedly and from several tickers at once.
    """
    now = now or timezone.now()
    with transaction.atomic():
        bounty = _lock_bounty(bounty_id)
        if bounty.status != BountyStatus.OPEN or bounty.deadline > now:
            return False
        pending = list(
            Claim.objects.select_for_update(of=("self",)).select_related("claimant")
            .filter(bounty=bounty, status=ClaimStatus.PENDING).order_by("id")
        )
        bounty.status = BountyStatus.EXPIRED
        bounty.save(update_fields=["status", "updated_at"])
        Claim.objects.filter(id__in=[c.id for c in pending]).update(status=ClaimStatus.REJECTED, updated_at=now)
        inbox.notify(
            bounty.poster, NotificationKind.BOUNTY_EXPIRED,
            f"'{bounty.title}' expired with its deadline. Post it again if you still need it.",
            f"bounty-expired:{bounty.pk}", bounty=bounty, now=now,
        )
        for c in pending:
            inbox.notify(
                c.claimant, NotificationKind.BOUNTY_EXPIRED,
                f"'{bounty.title}' expired before the poster chose anyone, so your claim was closed.",
                f"bounty-expired:{bounty.pk}:{c.pk}", bounty=bounty, claim=c, now=now,
            )
    return True


def sweep_expired(now=None, limit=50) -> int:
    """Expire up to ``limit`` overdue bounties, oldest deadline first. One transaction per bounty, so
    a failure on one neither blocks nor rolls back the others. Returns how many were expired."""
    now = now or timezone.now()
    ids = list(
        Bounty.objects.filter(status=BountyStatus.OPEN, deadline__lte=now)
        .order_by("deadline", "pk").values_list("pk", flat=True)[:limit]
    )
    done = 0
    for pk in ids:
        try:
            done += expire_bounty(pk, now)
        except Exception:  # noqa: BLE001 - keep sweeping; the next tick retries this one
            logger.exception("Could not expire bounty %s", pk)
    return done


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
