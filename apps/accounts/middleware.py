from django.contrib import messages
from django.contrib.auth import logout


class VerifiedEmailMiddleware:
    """Log out any authenticated user whose email is not verified (fail closed).

    Login already refuses unverified accounts, so this only fires when verification is lost
    after a session exists (a later ``change_email``, an admin-created account, a stale session
    that predates this rule). Checking per request rather than only at login means the rule
    cannot be bypassed by an old session cookie. ``request.user`` is already loaded by
    AuthenticationMiddleware, so this adds no query.

    Must sit after AuthenticationMiddleware and MessageMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        user = request.user
        if user.is_authenticated and user.email_verified_at is None:
            logout(request)
            messages.warning(request, "Confirm your email to continue. Check your inbox, or request a new link.")
        return self.get_response(request)
