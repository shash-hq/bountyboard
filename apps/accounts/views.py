from django.contrib import messages
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.bounties.models import Bounty, Claim
from .forms import LoginForm, SignupForm

User = get_user_model()


@ratelimit(key="ip", rate="10/m", method="POST", block=True)
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


@ratelimit(key="ip", rate="15/m", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def login_view(request):
    if request.user.is_authenticated:
        return redirect("/")

    next_url = request.GET.get("next") or request.POST.get("next") or "/"

    if request.method == "POST":
        form = LoginForm(request.POST)
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
