from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_not_required, login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST
from django.contrib.auth import views as auth_views
from django.utils.decorators import method_decorator
from django_ratelimit.core import is_ratelimited
from django_ratelimit.decorators import ratelimit
from django_ratelimit.exceptions import Ratelimited

from apps.bounties.models import Bounty, Claim
from .forms import ConfirmEmailForm, LoginForm, ResendVerificationForm, SignupForm
from .ratelimits import confirm_account, login_identifier, rate, reset_email
from .services import InvalidToken, UsernameTaken, confirm_email, peek_confirmation, send_verification_email, signup

User = get_user_model()


@login_not_required
@ratelimit(key="ip", rate=rate("signup_per_ip"), method="POST", block=True)
@require_http_methods(["GET", "POST"])
def signup_view(request):
    if request.user.is_authenticated:
        return redirect("board")

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            # Shared with resend: ONE per-address budget for every verification-type email, so
            # repeated signups cannot flood someone's inbox. Applies whatever the address is.
            if _email_budget(request):
                raise Ratelimited()
            try:
                signup(form.cleaned_data["username"], form.cleaned_data["email"], form.cleaned_data["password"])
            except UsernameTaken:
                form.add_error("username", "This username is already taken. Pick a different handle.")
            else:
                # Identical for a new address, an unverified one we replaced, and a verified
                # account (which gets a "you already have an account" email instead of a link).
                messages.success(request, f"Check your inbox at {form.cleaned_data['email']} for the next step.")
                return redirect("accounts:login")
    else:
        form = SignupForm(initial={"email": request.GET.get("email", "")[:254]})

    return render(request, "accounts/signup.html", {"form": form})


@login_not_required
@never_cache
@ratelimit(group="login-ip", key="ip", rate=rate("login_per_ip"), method="POST", block=True)
@require_http_methods(["GET", "POST"])
def verify_email_view(request, uid, token):
    """Email-verification page: the link plus the account password confirms the address.

    GET only renders the form and never changes state (mail scanners such as Outlook Safe Links
    and Gmail prefetch every URL in a message). POST is a password check, so it shares login's
    throttle: the same per-IP bucket, and the same per-account failure budget a username login
    uses. It must not become a way around login's rate limit.
    """
    user = peek_confirmation(uid, token)
    if user is None:
        return render(request, "accounts/verify_email_invalid.html", status=400)
    context = {"form": ConfirmEmailForm(), "email": user.email, "uid": uid, "token": token}

    if request.method == "POST":
        if _login_failure_limit(request, increment=False, key=confirm_account):
            raise Ratelimited()
        form = ConfirmEmailForm(request.POST)
        if form.is_valid():
            try:
                confirmed = confirm_email(uid, token, form.cleaned_data["password"])
            except InvalidToken:
                pass
            else:
                # login() rotates the session key for a session that had no user, so a session
                # id that existed before confirming is not carried into the logged-in state.
                login(request, confirmed, backend="django.contrib.auth.backends.ModelBackend")
                messages.success(request, f"Email confirmed. Welcome to Bounty Board, {confirmed.username}!")
                return redirect("board")
        _login_failure_limit(request, increment=True, key=confirm_account)
        context["error"] = "That link or password didn't work. Check your password and try again."
    return render(request, "accounts/verify_email.html", context)


@login_not_required
@ratelimit(key="ip", rate=rate("verify_resend_per_ip"), method="POST", block=True)
@require_http_methods(["GET", "POST"])
def resend_verification_view(request):
    """Send a fresh verification link. The response is identical whether or not the address
    has an unverified account (no account enumeration), and the limits apply either way, so
    one mailbox cannot be used to burn the daily email budget. The per-address budget is
    shared with signup."""
    if request.method == "POST":
        if _email_budget(request):
            raise Ratelimited()
        form = ResendVerificationForm(request.POST)
        if form.is_valid():
            user = User.objects.filter(email__iexact=form.cleaned_data["email"], is_active=True).first()
            if user is not None:
                send_verification_email(user.pk)  # no-op if already verified
            messages.info(request, "If that address belongs to an account that still needs confirming, we sent a new link. The older link no longer works.")
            return redirect("accounts:login")
    else:
        form = ResendVerificationForm()
    return render(request, "accounts/resend_verification.html", {"form": form})


def _email_budget(request):
    """Count one verification-type email against this address's budget; True if over it.
    Counted whether or not the address has an account, so the answer reveals nothing."""
    return is_ratelimited(
        request, group="verification-email", key=reset_email,
        rate=rate("verify_resend_per_email"), method="POST", increment=True,
    )


def _login_failure_limit(request, increment, key=login_identifier):
    return is_ratelimited(
        request, group="login-failures", key=key,
        rate=rate("login_failures_per_account"), method="POST", increment=increment,
    )


@login_not_required
@ratelimit(group="login-ip", key="ip", rate=rate("login_per_ip"), method="POST", block=True)
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("board")

    next_url = request.GET.get("next") or request.POST.get("next") or reverse("board")

    if request.method == "POST":
        # Only FAILED attempts count against an account (so a successful login never does, and
        # an attacker cannot lock a victim out by spamming correct-looking requests for them).
        # Once an account has too many recent failures, even the right password is refused.
        if _login_failure_limit(request, increment=False):
            raise Ratelimited()
        form = LoginForm(request.POST)
        if not form.is_valid():
            _login_failure_limit(request, increment=True)
        if form.is_valid():
            user = form.cleaned_data["user"]
            login(request, user)
            messages.success(request, f"Logged in as {user.username}.")
            
            # Security: ensure next URL is safe
            if url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
                return redirect(next_url)
            return redirect("board")
    else:
        form = LoginForm()

    return render(request, "accounts/login.html", {"form": form, "next": next_url})


@login_not_required
@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        username = request.user.username
        logout(request)
        messages.info(request, f"Logged out. See you next time, {username}!")
    return redirect("landing")


@login_required
def me_view(request):
    """User profile and personal activity dashboard."""
    user = request.user

    # Bounties posted by current user
    posted_bounties = (
        Bounty.objects.filter(poster=user)
        .select_related("accepted_claim__claimant")
        .prefetch_related("claims")
        .order_by("-created_at")
    )

    # Claims submitted by current user
    submitted_claims = (
        Claim.objects.filter(claimant=user)
        .select_related("bounty__poster")
        .order_by("-created_at")
    )

    open_bounties_count = Bounty.objects.filter(poster=user, status="OPEN", is_hidden=False).count()

    context = {
        "posted_bounties": posted_bounties,
        "submitted_claims": submitted_claims,
        "open_bounties_count": open_bounties_count,
        "is_moderator": user.groups.filter(name="Moderators").exists() or user.is_staff or user.has_perm("bounties.moderate"),
    }
    return render(request, "accounts/me.html", context)


@method_decorator(ratelimit(key="ip", rate=rate("password_reset_per_ip"), method="POST", block=True), name="dispatch")
@method_decorator(ratelimit(key=reset_email, rate=rate("password_reset_per_email"), method="POST", block=True), name="dispatch")
class RateLimitedPasswordResetView(auth_views.PasswordResetView):
    """Password reset sends email (300/day budget), so it is limited per target address and per IP.
    The limit applies whether or not the address has an account, so responses stay identical."""
