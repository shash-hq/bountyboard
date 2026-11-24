from django.conf import settings

from apps.bounties.policies import can_moderate


def global_ui_context(request):
    """Exposes global layout cookie and theme constants."""
    layout = request.COOKIES.get("bb_layout", "grid")
    if layout not in ("grid", "list"):
        layout = "grid"

    return {
        "bb_layout": layout,
        "can_moderate": can_moderate(request.user) if hasattr(request, "user") else False,
        "DEBUG": settings.DEBUG,
    }
