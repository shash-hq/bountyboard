from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
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
from .forms import LoginForm, ResendVerificationForm, SignupForm
from .services import InvalidToken, confirm_email, send_verification_email
from .ratelimits import login_identifier, rate, reset_email

User = get_user_model()


@ratelimit(key="ip", rate=rate("signup_per_ip"), method="POST", block=True)
@require_http_methods(["GET", "POST"])
def signup_view(request):
    if request.user.is_authenticated:
        return redirect("board")

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data["username"]
            email = form.cleaned_data["email"]
            password = form.cleaned_data["password"]
            user = User.objects.create_user(username=username, email=email, password=password)
            # No login here: the address is unproven until its owner clicks the emailed link.
            send_verification_email(user.pk)
            messages.success(request, f"Account created. We emailed a confirmation link to {user.email}. Confirm it to log in.")
            return redirect("accounts:login")
    else:
        form = SignupForm()

    return render(request, "accounts/signup.html", {"form": form})


@never_cache
@require_http_methods(["GET", "POST"])
def verify_email_view(request, token):
    """Email-verification landing page.

    GET only renders a button. Mail clients and link scanners (Gmail, Outlook SafeLinks)
    prefetch every URL in a message, so a GET that consumed the token would burn it before the
    human ever clicked. Only the POST, which scanners do not send, confirms.
    """
    if request.method == "POST":
        try:
            confirm_email(token)
        except InvalidToken:
            return render(request, "accounts/verify_email_invalid.html", status=400)
        messages.success(request, "Email confirmed. You can log in now.")
        return redirect("accounts:login")
    return render(request, "accounts/verify_email.html", {"token": token})


@ratelimit(key="ip", rate=rate("verify_resend_per_ip"), method="POST", block=True)
@ratelimit(key=reset_email, rate=rate("verify_resend_per_email"), method="POST", block=True)
@require_http_methods(["GET", "POST"])
def resend_verification_view(request):
    """Send a fresh verification link. The response is identical whether or not the address
    has an unverified account (no account enumeration), and the limits apply either way, so
    one mailbox cannot be used to burn the daily email budget."""
    if request.method == "POST":
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


def _login_failure_limit(request, increment):
    return is_ratelimited(
        request, group="login-failures", key=login_identifier,
        rate=rate("login_failures_per_account"), method="POST", increment=increment,
    )


@ratelimit(key="ip", rate=rate("login_per_ip"), method="POST", block=True)
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
