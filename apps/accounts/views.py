from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.contrib.auth import get_user_model, login, logout
from django.contrib.auth.decorators import login_not_required, login_required
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST
from django.contrib.auth import views as auth_views
from django.utils.decorators import method_decorator
from django_ratelimit.core import is_ratelimited
from django_ratelimit.decorators import ratelimit
from django_ratelimit.exceptions import Ratelimited

from apps.bounties.models import Bounty, Claim
from apps.bounties.exceptions import DomainError
from apps.bounties.policies import can_moderate, can_verify_students
from . import campus, desk
from .forms import ConfirmEmailForm, DeskVerifyForm, LoginForm, ResendVerificationForm, SignupForm
from .ratelimits import confirm_account, desk_verifier, login_identifier, rate, reset_email
from .campus import needs_reg_no
from .identity import InvalidCardName, InvalidRegNo
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
    ask_reg_no = needs_reg_no(user)
    context = {"form": ConfirmEmailForm(ask_reg_no=ask_reg_no), "email": user.email, "uid": uid, "token": token}

    if request.method == "POST":
        if _login_failure_limit(request, increment=False, key=confirm_account):
            raise Ratelimited()
        form = ConfirmEmailForm(request.POST, ask_reg_no=ask_reg_no)
        context["form"] = form
        if form.is_valid():
            try:
                confirmed = confirm_email(
                    uid, token, form.cleaned_data["password"], form.cleaned_data.get("reg_no", "")
                )
            except InvalidRegNo:
                # Only reachable after the password was right, so it is not a failed login attempt
                # and doesn't count against the throttle.
                form.add_error("reg_no", InvalidRegNo.messages[0])
                return render(request, "accounts/verify_email.html", context)
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


def _campus_context(user):
    """The user's own verification record and, if they are not verified, the desk presence code.

    Deliberately omits ``card_name_enc`` (the manager defers it) and the registration number.
    """
    now = timezone.now()
    active = campus.active_verifications(now).filter(user=user).first()
    ctx = {"campus_verification": active, "presence_code": None}
    if active is not None:
        ctx["campus_expires_at"] = active.verified_at + timedelta(days=settings.VERIFICATION_TTL_DAYS)
    elif user.email_verified_at is not None:
        ctx["last_verification"] = user.campus_verifications.order_by("-verified_at").first()
        ctx["presence_code"] = desk.presence_code(user.pk, now)
    return ctx


@login_required
@ratelimit(group="desk", key=desk_verifier, rate=rate("desk_per_verifier"), method="POST", block=True)
@never_cache
@require_http_methods(["GET", "POST"])
def desk_view(request):
    """The verifier desk: a volunteer who has seen the student's physical ID card marks them verified.

    Gated on ``accounts.verify_student`` alone (see ``can_verify_students``), so a verifier gets no
    moderation or contact access from this page. Plain POST + redirect; no JS. The rate limit
    counts every submission (a wrong presence code included), which is what stops code guessing;
    the service separately caps successful verifications per verifier.
    """
    if not can_verify_students(request.user):
        raise PermissionDenied("You are not allowed to verify students.")
    form = DeskVerifyForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        try:
            desk.verify_in_person(request.user.pk, cd["username"], cd["code"], cd["reg_no"], cd["card_name"])
        except InvalidRegNo:
            form.add_error("reg_no", InvalidRegNo.messages[0])
        except InvalidCardName:
            form.add_error("card_name", InvalidCardName.messages[0])
        except DomainError as e:
            form.add_error(None, e.messages[0])
        else:
            # Name the student only; never echo the registration number or the card name.
            messages.success(request, f"{cd['username']} is now campus-verified.")
            return redirect("accounts:desk")
    return render(request, "accounts/desk.html", {"form": form})


@login_required
@never_cache
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
        "is_moderator": can_moderate(user),
        **_campus_context(user),
    }
    return render(request, "accounts/me.html", context)


@method_decorator(ratelimit(key="ip", rate=rate("password_reset_per_ip"), method="POST", block=True), name="dispatch")
@method_decorator(ratelimit(key=reset_email, rate=rate("password_reset_per_email"), method="POST", block=True), name="dispatch")
class RateLimitedPasswordResetView(auth_views.PasswordResetView):
    """Password reset sends email (300/day budget), so it is limited per target address and per IP.
    The limit applies whether or not the address has an account, so responses stay identical."""
