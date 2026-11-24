"""Domain exceptions for the accounts services. Views render ``.messages``, never ``str(e)`` of a
ValidationError.

They share PR 2's base (``apps.bounties.exceptions.DomainError``), so a view that catches
``DomainError`` handles bounty and accounts failures alike, and ``NotPermitted`` is the one class for
"you may not" (still a 403 if a view forgets to catch it).
"""
from apps.bounties.exceptions import DomainError, NotPermitted  # noqa: F401 (NotPermitted re-exported)


class AccountsError(DomainError):
    """Base for expected, user-explainable failures of an accounts service call."""


class InvalidPresenceCode(AccountsError):
    """The code is wrong or expired. One message for both, so it can't be probed."""


class VerifierRateLimited(AccountsError):
    """The verifier hit their hourly or daily cap."""


class SubjectNotFound(AccountsError):
    """No such user, or one that cannot be verified right now."""


class InvalidState(AccountsError):
    """The row is not in a state where this action applies (re-read under the lock)."""


class DeletionBlocked(AccountsError):
    """The account has work in flight that must be finished or cancelled first."""
