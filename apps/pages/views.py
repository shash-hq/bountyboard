from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_POST

from config.http import safe_referer


@require_GET
def healthz(request):
    """Health check endpoint for deployment monitoring."""
    return HttpResponse("OK", content_type="text/plain; charset=utf-8")


@require_GET
def robots_txt(request):
    """Serve robots.txt directly."""
    lines = [
        "User-agent: *",
        "Allow: /",
        "Disallow: /admin/",
        "Disallow: /claims/",
        f"Sitemap: {request.build_absolute_uri('/sitemap.xml')}",
    ]
    return HttpResponse("\n".join(lines), content_type="text/plain")


@require_GET
def about(request):
    return render(request, "pages/about.html")


@require_GET
def terms(request):
    return render(request, "pages/terms.html")


@require_GET
def privacy(request):
    return render(request, "pages/privacy.html")


@require_GET
def guidelines(request):
    return render(request, "pages/guidelines.html")


@require_GET
def styleguide(request):
    """DEBUG-only styleguide showcasing all neo-brutalist tokens & components."""
    if not settings.DEBUG:
        raise Http404("Styleguide is only accessible in DEBUG mode.")
    return render(request, "pages/styleguide.html")


@require_POST
def set_layout(request):
    """Sets bb_layout cookie (grid | list) and redirects back."""
    layout = request.POST.get("layout", "grid")
    if layout not in ("grid", "list"):
        layout = "grid"
    response = HttpResponseRedirect(safe_referer(request, fallback="/"))
    # 30 day max_age
    response.set_cookie("bb_layout", layout, max_age=60 * 60 * 24 * 30, samesite="Lax")
    return response


# Error handlers
def handler400(request, exception=None):
    return render(request, "errors/400.html", status=400)


def handler403(request, exception=None):
    return render(request, "errors/403.html", status=403)


def handler404(request, exception=None):
    return render(request, "errors/404.html", status=404)


def handler500(request):
    return render(request, "errors/500.html", status=500)
