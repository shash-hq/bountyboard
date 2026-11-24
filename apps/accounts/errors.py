"""Domain exceptions for the accounts services. Views render ``.messages``, never ``str(e)`` of a
ValidationError. Shaped like ``apps.bounties.exceptions.DomainError`` (PR 2) so they can be
re-parented onto it once that merges."""
from django.core.exceptions import PermissionDenied


class AccountsError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.messages = [message]

    def __str__(self) -> str:
        return self.messages[0]


class NotPermitted(AccountsError, PermissionDenied):
    """The actor may not do this (a forgotten catch becomes a 403, not a 500)."""


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
