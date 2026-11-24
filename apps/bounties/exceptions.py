"""Domain exceptions raised by services. Views catch DomainError and render ``.messages``.

Services signal "you may not" and "not in that state" with these, not Django's
ValidationError (which is for form/model field validation and renders as "['...']").
"""
from django.core.exceptions import PermissionDenied


class DomainError(Exception):
    """Base for expected, user-explainable failures of a service call."""

    def __init__(self, message: str):
        super().__init__(message)
        self.messages = [message]  # same shape as ValidationError.messages, so views stay uniform

    def __str__(self) -> str:
        return self.messages[0]


class InvalidTransition(DomainError):
    """The requested change is not valid for the row's CURRENT state.

    Raised after the row lock is taken and the status re-read, so it also covers
    "someone else changed this a moment ago".
    """


class NotPermitted(DomainError, PermissionDenied):
    """The actor is not allowed to do this.

    Also a PermissionDenied so that, if a view forgets to catch it, Django answers 403
    instead of a 500.
    """


class LimitReached(DomainError):
    """A per-user cap (e.g. live OPEN bounties) is already full."""
