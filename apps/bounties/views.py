from django.core.paginator import Paginator
from django.db.models import Count, Q
from django.shortcuts import render

from .models import Bounty, BountyStatus, Category


def board_view(request):
    """Main live board with search, category/status filtering, and sorting."""
    # Query parameters
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

    # Search filter
    if q:
        queryset = queryset.filter(
            Q(title__icontains=q) | Q(description__icontains=q)
        )

    # Category filter
    if category and category in Category.values:
        queryset = queryset.filter(category=category)

    # Status filter
    if status == "OPEN":
        queryset = queryset.filter(status=BountyStatus.OPEN)
    elif status == "CLAIMED":
        queryset = queryset.filter(status=BountyStatus.CLAIMED)
    elif status == "DONE":
        queryset = queryset.filter(status=BountyStatus.DONE)
    elif status == "CANCELLED":
        queryset = queryset.filter(status=BountyStatus.CANCELLED)

    # Sorting
    if sort == "reward_high":
        queryset = queryset.order_by("-reward", "-created_at")
    elif sort == "reward_low":
        queryset = queryset.order_by("reward", "-created_at")
    elif sort == "deadline_soon":
        queryset = queryset.order_by("deadline")
    else:  # default 'newest'
        queryset = queryset.order_by("-created_at")

    # Pagination: 12 items per page
    paginator = Paginator(queryset, 12)
    page_obj = paginator.get_page(page_number)

    # Retrieve last 5 recently viewed bounties from session
    recent_ids = request.session.get("recently_viewed_bounties", [])
    recent_bounties = []
    if recent_ids:
        # Preserve order of recent_ids
        bounties_map = {
            b.id: b
            for b in Bounty.objects.filter(id__in=recent_ids, is_hidden=False)
        }
        recent_bounties = [bounties_map[bid] for bid in recent_ids if bid in bounties_map]

    # Newest ticker bounties
    ticker_bounties = Bounty.objects.filter(is_hidden=False, status=BountyStatus.OPEN).order_by("-created_at")[:6]

    context = {
        "page_obj": page_obj,
        "bounties": page_obj.object_list,
        "recent_bounties": recent_bounties,
        "ticker_bounties": ticker_bounties,
        "q": q,
        "category": category,
        "status": status,
        "sort": sort,
        "categories": Category.choices,
        "statuses": BountyStatus.choices,
    }
    return render(request, "bounties/board.html", context)
