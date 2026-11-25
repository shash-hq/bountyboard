"""Moderator console for identity: lookup, one user's record, and the audited actions on it.

Everything here is gated on ``can_moderate`` (never on the verifier permission) and each action
calls a service that re-checks permission on the locked row and writes the AuditEvent. Plain
forms, no JS. Responses that carry revealed plaintext (contact, card name) are rendered directly
from the POST, never put in a session, flash message or redirect URL, and are ``never_cache``.
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from apps.bounties.exceptions import DomainError
from apps.bounties.policies import can_moderate

from . import moderation
from .forms import (
    ModReasonForm, RegNoLookupForm, ReleaseIdentityForm, ResolveReviewForm, RevokeForm, StaffNoteForm,
)
from .identity import InvalidRegNo
from .models import AuditEvent, CampusVerification, IdentityReview, ReviewStatus, StudentIdentity, User

REVEAL_RATE = "30/h"


def _require_moderator(request):
    if not can_moderate(request.user):
        raise PermissionDenied("You do not have moderator permissions.")


@login_required
@never_cache
@ratelimit(key="user", rate=REVEAL_RATE, method="POST", block=True)
@require_http_methods(["GET", "POST"])
def mod_index_view(request):
    """Find an account by registration number. The number is hashed server-side and never stored."""
    _require_moderator(request)
    form = RegNoLookupForm(request.POST or None)
    result = searched = None
    if request.method == "POST" and form.is_valid():
        try:
            result = moderation.lookup_by_reg_no(request.user.pk, form.cleaned_data["reg_no"], form.cleaned_data["reason"])
            searched = True
        except InvalidRegNo:
            form.add_error("reg_no", InvalidRegNo.messages[0])
        except DomainError as e:
            form.add_error(None, e.messages[0])
    return render(request, "accounts/mod_index.html", {"form": form, "result": result, "searched": searched})


def _user_context(subject, **extra):
    return {
        "subject": subject,
        "identity": StudentIdentity.objects.filter(user=subject).first(),
        "verifications": CampusVerification.objects.filter(user=subject).order_by("-verified_at", "-pk")[:5],
        "audit": AuditEvent.objects.filter(subject=subject).select_related("actor").order_by("-created_at", "-pk")[:20],
        # Several forms share field names; auto_id keeps every id (and its label) unique on the page.
        "contact_form": ModReasonForm(auto_id="contact-%s"),
        "card_form": ModReasonForm(auto_id="card-%s"),
        "revoke_form": RevokeForm(auto_id="revoke-%s"),
        "release_form": ReleaseIdentityForm(auto_id="release-%s"),
        "staff_form": StaffNoteForm(auto_id="staff-%s"),
        **extra,
    }


@login_required
@never_cache
def mod_user_view(request, pk):
    _require_moderator(request)
    subject = get_object_or_404(User, pk=pk)
    return render(request, "accounts/mod_user.html", _user_context(subject))


@login_required
@never_cache
@require_POST
@ratelimit(key="user", rate=REVEAL_RATE, method="POST", block=True)
def mod_reveal_contact_view(request, pk):
    _require_moderator(request)
    subject = get_object_or_404(User, pk=pk)
    form = ModReasonForm(request.POST)
    extra = {}
    if form.is_valid():
        try:
            email, handle = moderation.reveal_contact_for_moderation(request.user.pk, subject.pk, form.cleaned_data["reason"])
            extra["contact"] = {"email": email, "handle": handle}
        except DomainError as e:
            messages.error(request, e.messages[0])
    else:
        messages.error(request, "Say why you need the contact details.")
    return render(request, "accounts/mod_user.html", _user_context(subject, **extra))


@login_required
@never_cache
@require_POST
@ratelimit(key="user", rate=REVEAL_RATE, method="POST", block=True)
def mod_reveal_card_name_view(request, pk):
    _require_moderator(request)
    subject = get_object_or_404(User, pk=pk)
    form = ModReasonForm(request.POST)
    extra = {}
    if form.is_valid():
        try:
            extra["card_name"] = moderation.reveal_card_name(request.user.pk, subject.pk, form.cleaned_data["reason"])
        except DomainError as e:
            messages.error(request, e.messages[0])
    else:
        messages.error(request, "Say why you need the card name.")
    return render(request, "accounts/mod_user.html", _user_context(subject, **extra))


@login_required
@require_POST
def mod_revoke_view(request, pk):
    _require_moderator(request)
    subject = get_object_or_404(User, pk=pk)
    form = RevokeForm(request.POST)
    if form.is_valid():
        try:
            moderation.revoke_verification(request.user.pk, subject.pk, form.cleaned_data["reason_code"], form.cleaned_data["note"])
            messages.success(request, f"{subject.username}'s verification was revoked.")
        except DomainError as e:
            messages.error(request, e.messages[0])
    else:
        messages.error(request, "Choose a valid reason (and keep the note under 500 characters).")
    return redirect("accounts:mod_user", pk=subject.pk)


@login_required
@require_POST
def mod_release_identity_view(request, pk):
    _require_moderator(request)
    subject = get_object_or_404(User, pk=pk)
    form = ReleaseIdentityForm(request.POST)
    if form.is_valid():
        try:
            moderation.release_identity(request.user.pk, subject.pk, form.cleaned_data["note"])
            messages.success(request, "Registration-number binding released.")
        except DomainError as e:
            messages.error(request, e.messages[0])
    else:
        messages.error(request, "Say why the identity is being released.")
    return redirect("accounts:mod_user", pk=subject.pk)


@login_required
@never_cache
def mod_reviews_view(request):
    """Open registration-number collisions, oldest first."""
    _require_moderator(request)
    reviews = list(IdentityReview.objects.filter(status=ReviewStatus.OPEN).select_related("claimant").order_by("created_at", "pk"))
    holders = {
        i.reg_no_hmac: i.user
        for i in StudentIdentity.objects.filter(reg_no_hmac__in=[r.claimed_hmac for r in reviews]).select_related("user")
    }
    for r in reviews:
        r.holder = holders.get(r.claimed_hmac)
        r.form = ResolveReviewForm(auto_id=f"review{r.pk}-%s")
    return render(request, "accounts/mod_reviews.html", {"reviews": reviews})


@login_required
@require_POST
def mod_resolve_review_view(request, pk):
    _require_moderator(request)
    form = ResolveReviewForm(request.POST)
    if form.is_valid():
        try:
            moderation.resolve_identity_review(request.user.pk, pk, form.cleaned_data["resolution"], form.cleaned_data["note"])
            messages.success(request, "Review resolved.")
        except DomainError as e:
            messages.error(request, e.messages[0])
    else:
        messages.error(request, "Choose a decision and say why.")
    return redirect("accounts:mod_reviews")


def _staff_action(request, pk, service, done):
    _require_moderator(request)
    subject = get_object_or_404(User, pk=pk)
    form = StaffNoteForm(request.POST)
    if form.is_valid():
        try:
            service(request.user.pk, subject.pk, form.cleaned_data["note"])
            messages.success(request, done.format(user=subject.username))
        except DomainError as e:
            messages.error(request, e.messages[0])
    else:
        messages.error(request, "The note is too long.")
    return redirect("accounts:mod_user", pk=subject.pk)


@login_required
@require_POST
def mod_grant_staff_view(request, pk):
    return _staff_action(request, pk, moderation.grant_staff, "{user} now has the staff role.")


@login_required
@require_POST
def mod_revoke_staff_view(request, pk):
    return _staff_action(request, pk, moderation.revoke_staff, "{user} no longer has the staff role.")
