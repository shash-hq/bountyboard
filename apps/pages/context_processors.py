from django.conf import settings


def global_ui_context(request):
    """Exposes global layout cookie and theme constants."""
    layout = request.COOKIES.get("bb_layout", "grid")
    if layout not in ("grid", "list"):
        layout = "grid"

    return {
        "bb_layout": layout,
        "DEBUG": settings.DEBUG,
    }
