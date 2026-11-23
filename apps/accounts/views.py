from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST
from django.contrib.auth import views as auth_views
from django.utils.decorators import method_decorator
from django_ratelimit.core import is_ratelimited
from django_ratelimit.decorators import ratelimit
from django_ratelimit.exceptions import Ratelimited

from apps.bounties.models import Bounty, Claim
from .forms import LoginForm, SignupForm
from .ratelimits import login_identifier, rate, reset_email

User = get_user_model()


@ratelimit(key="ip", rate=rate("signup_per_ip"), method="POST", block=True)
@require_http_methods(["GET", "POST"])
def signup_view(request):
    if request.user.is_authenticated:
        return redirect("/")

    if request.method == "POST":
        form = SignupForm(request.POST)
        if form.is_valid():
            username = form.cleaned_data["username"]
            email = form.cleaned_data["email"]
            password = form.cleaned_data["password"]
            user = User.objects.create_user(username=username, email=email, password=password)
            login(request, user)
            messages.success(request, f"Welcome to Bounty Board, {user.username}! Your campus account is live.")
            return redirect("/")
    else:
        form = SignupForm()

    return render(request, "accounts/signup.html", {"form": form})


def _login_failure_limit(request, increment):
    return is_ratelimited(
        request, group="login-failures", key=login_identifier,
        rate=rate("login_failures_per_account"), method="POST", increment=increment,
    )


@ratelimit(key="ip", rate=rate("login_per_ip"), method="POST", block=True)
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("/")

    next_url = request.GET.get("next") or request.POST.get("next") or "/"

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
            return redirect("/")
    else:
        form = LoginForm()

    return render(request, "accounts/login.html", {"form": form, "next": next_url})


@require_POST
def logout_view(request):
    if request.user.is_authenticated:
        username = request.user.username
        logout(request)
        messages.info(request, f"Logged out. See you next time, {username}!")
    return redirect("/")


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
