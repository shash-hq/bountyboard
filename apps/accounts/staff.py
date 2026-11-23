"""The staff role: faculty and staff who may post tasks. Orthogonal to campus verification.

It is granted only by a moderator (a later commit) or by ``STAFF_EMAIL_ALLOWLIST`` at email
confirmation, never inferred from the email domain. The badge shown on bounties additionally
requires an active campus verification (checked where it is rendered), so a lapsed or revoked
verification hides it without touching this role.
"""
from django.conf import settings
from django.utils import timezone

from .models import AuditEvent, StaffSource


def is_allowlisted(email: str) -> bool:
    return isinstance(email, str) and email.strip().lower() in settings.STAFF_EMAIL_ALLOWLIST


def grant_if_allowlisted(user, now=None) -> bool:
    """Grant the staff role to ``user`` if their (just-confirmed) address is allowlisted.

    Call with the user row locked and only after the address is proven (email confirmation): an
    allowlisted address typed into a signup form proves nothing. Mutates ``user`` in memory; the
    caller saves ``staff_since`` and ``staff_source``. An existing grant (moderator or earlier
    allowlist) is left alone, so a moderator's grant is never relabelled ALLOWLIST.
    """
    if user.staff_source is not None or not is_allowlisted(user.email):
        return False
    now = now or timezone.now()
    user.staff_since, user.staff_source = now, StaffSource.ALLOWLIST
    AuditEvent.objects.create(subject=user, action="STAFF_GRANTED", reason=StaffSource.ALLOWLIST, created_at=now)
    return True
