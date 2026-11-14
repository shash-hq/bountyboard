from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST
from django_ratelimit.decorators import ratelimit

from .forms import BountyForm, ClaimForm, ReportForm
from .models import Bounty, BountyStatus, Category, Claim, ClaimStatus, Report
from .notifications import send_claim_received_notification
from .services import (
    accept_claim,
    cancel_bounty,
    mark_bounty_done,
    reject_claim,
    withdraw_claim,
)


def board_view(request):
    """Main live board with search, category/status filtering, and sorting."""
    q = request.GET.get("q", "").strip()
    category = request.GET.get("category", "").strip()
    status = request.GET.get("status", "").strip()
    sort = request.GET.get("sort", "newest").strip()
    page_number = request.GET.get("page", 1)

    queryset = (
        Bounty.objects.filter(is_hidden=False)
        .select_related("poster")
        .annotate(claim_count=Count("claims"))
    )

    if q:
        queryset = queryset.filter(
            Q(title__icontains=q) | Q(description__icontains=q)
        )

    if category and category in Category.values:
        queryset = queryset.filter(category=category)

    if status == "OPEN":
        queryset = queryset.filter(status=BountyStatus.OPEN)
    elif status == "CLAIMED":
        queryset = queryset.filter(status=BountyStatus.CLAIMED)
    elif status == "DONE":
        queryset = queryset.filter(status=BountyStatus.DONE)
    elif status == "CANCELLED":
        queryset = queryset.filter(status=BountyStatus.CANCELLED)

    if sort == "reward_high":
        queryset = queryset.order_by("-reward", "-created_at")
    elif sort == "reward_low":
        queryset = queryset.order_by("reward", "-created_at")
    elif sort == "deadline_soon":
        queryset = queryset.order_by("deadline")
    else:  # default 'newest'
        queryset = queryset.order_by("-created_at")

    paginator = Paginator(queryset, 12)
    page_obj = paginator.get_page(page_number)

    # Session: last 5 recently viewed bounties
    recent_ids = request.session.get("recently_viewed_bounties", [])
    recent_bounties = []
    if recent_ids:
        bounties_map = {
            b.id: b
            for b in Bounty.objects.filter(id__in=recent_ids, is_hidden=False)
        }
        recent_bounties = [bounties_map[bid] for bid in recent_ids if bid in bounties_map]

    context = {
        "page_obj": page_obj,
        "bounties": page_obj.object_list,
        "recent_bounties": recent_bounties,
        "q": q,
        "category": category,
        "status": status,
        "sort": sort,
        "categories": Category.choices,
        "statuses": BountyStatus.choices,
    }
    return render(request, "bounties/board.html", context)


def bounty_detail_view(request, pk, slug):
    """Bounty detail page showing description, deadline, reward, claims, and actions."""
    bounty = get_object_or_404(
        Bounty.objects.select_related("poster", "accepted_claim__claimant"),
        pk=pk,
    )

    # Hidden bounty check: only poster and moderators can view
    if bounty.is_hidden:
        can_moderate = (
            request.user.is_authenticated
            and (request.user == bounty.poster or request.user.has_perm("bounties.moderate"))
        )
        if not can_moderate:
            raise Http404("This bounty has been hidden by moderators.")

    # Track recently viewed in session (last 5, distinct, most recent first)
    recent = request.session.get("recently_viewed_bounties", [])
    if pk in recent:
        recent.remove(pk)
    recent.insert(0, pk)
    request.session["recently_viewed_bounties"] = recent[:5]
    request.session.modified = True

    user_claim = None
    if request.user.is_authenticated:
        user_claim = bounty.claims.filter(claimant=request.user).first()

    # Claims list: poster sees all claims; other users do not see others' claims
    claims = []
    if request.user == bounty.poster or (request.user.is_authenticated and request.user.has_perm("bounties.moderate")):
        claims = bounty.claims.select_related("claimant").order_by("-created_at")

    claim_form = ClaimForm() if bounty.can_claim(request.user) else None
    report_form = ReportForm() if request.user.is_authenticated and request.user != bounty.poster else None

    context = {
        "bounty": bounty,
        "claims": claims,
        "user_claim": user_claim,
        "claim_form": claim_form,
        "report_form": report_form,
        "can_edit": bounty.can_edit(request.user),
        "is_poster": request.user == bounty.poster,
    }
    return render(request, "bounties/detail.html", context)


@login_required
@ratelimit(key="user", rate="10/h", method="POST", block=True)
@require_http_methods(["GET", "POST"])
def bounty_create_view(request):
    """Post a new bounty. Enforces maximum 5 OPEN bounties per user."""
    # Check current open count
    open_count = Bounty.objects.filter(
        poster=request.user, status=BountyStatus.OPEN, is_hidden=False
    ).count()
    if open_count >= 5:
        messages.error(
            request,
            "You already have 5 active OPEN bounties. Complete or cancel one before posting more.",
        )
        return redirect("/me/")

    if request.method == "POST":
        form = BountyForm(request.POST)
        if form.is_valid():
            bounty = form.save(commit=False)
            bounty.poster = request.user
            try:
                bounty.save()
                messages.success(request, f"Bounty '{bounty.title}' is now live on the board!")
                return redirect(f"/bounties/{bounty.pk}/{bounty.slug}/")
            except ValidationError as e:
                for field, errs in e.message_dict.items():
                    for err in errs:
                        form.add_error(field if field != "__all__" else None, err)
    else:
        form = BountyForm()

    return render(request, "bounties/form.html", {"form": form, "action": "create"})


@login_required
@require_http_methods(["GET", "POST"])
def bounty_edit_view(request, pk):
    """Edit an existing bounty. Allowed only for the poster while OPEN."""
    bounty = get_object_or_404(Bounty, pk=pk)

    if request.user != bounty.poster:
        raise PermissionDenied("You do not have permission to edit this bounty.")

    if bounty.status != BountyStatus.OPEN or bounty.is_expired:
        messages.error(request, "Only active OPEN bounties can be edited.")
        return redirect(f"/bounties/{bounty.pk}/{bounty.slug}/")

    if request.method == "POST":
        form = BountyForm(request.POST, instance=bounty)
        if form.is_valid():
            bounty = form.save()
            messages.success(request, f"Bounty '{bounty.title}' updated successfully.")
            return redirect(f"/bounties/{bounty.pk}/{bounty.slug}/")
    else:
        form = BountyForm(instance=bounty)

    return render(
        request,
        "bounties/form.html",
        {"form": form, "bounty": bounty, "action": "edit"},
    )


@require_POST
@login_required
def bounty_done_view(request, pk):
    """Poster marks an in-progress claimed bounty as DONE."""
    try:
        bounty = mark_bounty_done(pk, request.user)
        messages.success(request, f"Bounty '{bounty.title}' marked as DONE! Offline settlement confirmed.")
    except (PermissionDenied, ValidationError) as e:
        messages.error(request, str(e))
    
    bounty = get_object_or_404(Bounty, pk=pk)
    return redirect(f"/bounties/{bounty.pk}/{bounty.slug}/")


@require_POST
@login_required
def bounty_cancel_view(request, pk):
    """Poster cancels an OPEN bounty."""
    try:
        bounty = cancel_bounty(pk, request.user)
        messages.info(request, f"Bounty '{bounty.title}' has been cancelled.")
    except (PermissionDenied, ValidationError) as e:
        messages.error(request, str(e))

    bounty = get_object_or_404(Bounty, pk=pk)
    return redirect(f"/bounties/{bounty.pk}/{bounty.slug}/")


@require_POST
@login_required
@ratelimit(key="user", rate="20/h", method="POST", block=True)
def bounty_claim_view(request, pk):
    """Submit a claim on a bounty."""
    bounty = get_object_or_404(Bounty, pk=pk)

    if not bounty.can_claim(request.user):
        messages.error(request, "You cannot claim this bounty.")
        return redirect(f"/bounties/{bounty.pk}/{bounty.slug}/")

    form = ClaimForm(request.POST)
    if form.is_valid():
        claim = form.save(commit=False)
        claim.bounty = bounty
        claim.claimant = request.user
        claim.status = ClaimStatus.PENDING
        try:
            claim.save()
            send_claim_received_notification(claim)
            messages.success(
                request,
                f"Your claim on '{bounty.title}' was submitted! The poster will review your contact handle.",
            )
        except ValidationError as e:
            messages.error(request, str(e))
    else:
        for errs in form.errors.values():
            for err in errs:
                messages.error(request, err)

    return redirect(f"/bounties/{bounty.pk}/{bounty.slug}/")


@require_POST
@login_required
def claim_accept_view(request, pk):
    """Accept a pending claim."""
    claim = get_object_or_404(Claim.objects.select_related("bounty"), pk=pk)
    try:
        accept_claim(claim.pk, request.user)
        messages.success(
            request,
            f"Accepted claim by {claim.claimant.username}! You can now coordinate via their contact handle: {claim.contact}",
        )
    except (PermissionDenied, ValidationError) as e:
        messages.error(request, str(e))

    return redirect(f"/bounties/{claim.bounty.pk}/{claim.bounty.slug}/")


@require_POST
@login_required
def claim_reject_view(request, pk):
    """Reject a pending claim."""
    claim = get_object_or_404(Claim.objects.select_related("bounty"), pk=pk)
    try:
        reject_claim(claim.pk, request.user)
        messages.info(request, f"Rejected claim by {claim.claimant.username}.")
    except (PermissionDenied, ValidationError) as e:
        messages.error(request, str(e))

    return redirect(f"/bounties/{claim.bounty.pk}/{claim.bounty.slug}/")


@require_POST
@login_required
def claim_withdraw_view(request, pk):
    """Claimant withdraws their own pending claim."""
    claim = get_object_or_404(Claim.objects.select_related("bounty"), pk=pk)
    try:
        withdraw_claim(claim.pk, request.user)
        messages.info(request, "Your claim was withdrawn.")
    except (PermissionDenied, ValidationError) as e:
        messages.error(request, str(e))

    return redirect("/me/")


@require_POST
@login_required
@ratelimit(key="user", rate="10/h", method="POST", block=True)
def bounty_report_view(request, pk):
    """File a report against a bounty."""
    bounty = get_object_or_404(Bounty, pk=pk)
    form = ReportForm(request.POST)
    if form.is_valid():
        report = form.save(commit=False)
        report.bounty = bounty
        report.reporter = request.user
        report.save()
        messages.success(request, "Report filed. Campus moderators will review this listing.")
    else:
        messages.error(request, "Unable to file report. Check the form.")

    return redirect(f"/bounties/{bounty.pk}/{bounty.slug}/")
