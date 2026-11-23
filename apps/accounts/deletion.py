"""Rules for deleting an account.

Deleting an account cascades to its ``StudentIdentity``, which frees the registration number. For a
student whose verification was revoked as invalid or for a policy violation, that would be a way
back in: delete the account, sign up again, verify with the same card. So such an account must not
be deletable by its owner.

``ensure_self_deletion_allowed`` is the single check any self-service deletion path must call. No
such path exists yet. The full fix (nullable ``StudentIdentity.user``, a deletion service that keeps
the HMAC tombstone and ban state) is a follow-up PR and will call this first. Until then the only
code that deletes users is the purge/replace of stale *unverified* signups, which already refuses any
account that owns a row (``services.owns_rows``), and a banned user owns rows.
"""
from .errors import NotPermitted
from .models import CampusVerification, RevokeReason

# LEFT_CAMPUS is deliberately not here: leaving campus is not misconduct, and those users may delete.
BLOCKING_REASONS = (RevokeReason.VERIFICATION_INVALID, RevokeReason.POLICY_VIOLATION)


def blocks_self_deletion(user) -> bool:
    """True if any of the user's verifications was ever revoked as invalid or for a violation.

    "Ever", not "latest": a later re-verification does not wipe a ban from the record.
    """
    return CampusVerification.objects.filter(user=user, revoke_reason__in=BLOCKING_REASONS).exists()


def ensure_self_deletion_allowed(user) -> None:
    if blocks_self_deletion(user):
        raise NotPermitted(
            "This account can't be deleted online because of a past verification issue. "
            "Please contact support and we'll help."
        )
