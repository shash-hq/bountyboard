from django.conf import settings
from django.contrib.auth.decorators import login_not_required
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_POST

from config.http import safe_referer


@login_not_required
@require_GET
def healthz(request):
    """Health check endpoint for deployment monitoring."""
    return HttpResponse("OK", content_type="text/plain; charset=utf-8")


@login_not_required
@require_GET
def robots_txt(request):
    """Serve robots.txt directly."""
    lines = [
        # Public pages only: the landing page, about, terms, privacy and guidelines. Everything
        # else needs a login, so there is nothing for a crawler to index there.
        "User-agent: *",
        "Disallow: /admin/",
        "Disallow: /accounts/",
        "Disallow: /board/",
        "Disallow: /bounties/",
        "Disallow: /claims/",
        "Disallow: /internal/",
        "Disallow: /me/",
        f"Sitemap: {request.build_absolute_uri('/sitemap.xml')}",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


@login_not_required
@require_GET
def landing(request):
    """Public front door. Signed-in users go straight to the board."""
    if request.user.is_authenticated:
        return redirect("board")
    return render(request, "pages/landing.html")


@login_not_required
@require_GET
def about(request):
    return render(request, "pages/about.html")


@login_not_required
@require_GET
def terms(request):
    return render(request, "pages/terms.html")


@login_not_required
@require_GET
def privacy(request):
    return render(request, "pages/privacy.html")


@login_not_required
@require_GET
def guidelines(request):
    return render(request, "pages/guidelines.html")


@require_GET
def styleguide(request):
    """DEBUG-only styleguide showcasing all neo-brutalist tokens & components."""
    if not settings.DEBUG:
        raise Http404("Styleguide is only accessible in DEBUG mode.")
    return render(request, "pages/styleguide.html")


@login_not_required
@require_POST
def set_layout(request):
    """Sets bb_layout cookie (grid | list) and redirects back."""
    layout = request.POST.get("layout", "grid")
    if layout not in ("grid", "list"):
        layout = "grid"
    response = HttpResponseRedirect(safe_referer(request, fallback=reverse("landing")))
    # 30 day max_age
    response.set_cookie("bb_layout", layout, max_age=60 * 60 * 24 * 30, samesite="Lax")
    return response


# Error handlers
def ratelimited(request, exception=None):
    """Rate limit hit: HTTP 429 (not 403, which means 'you may never do this')."""
    return render(request, "errors/429.html", status=429)


def handler400(request, exception=None):
    return render(request, "errors/400.html", status=400)


def handler403(request, exception=None):
    return render(request, "errors/403.html", status=403)


def handler404(request, exception=None):
    return render(request, "errors/404.html", status=404)


def handler500(request):
    return render(request, "errors/500.html", status=500)
