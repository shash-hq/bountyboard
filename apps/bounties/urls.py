from django.urls import path
from . import views

app_name = "bounties"

urlpatterns = [
    path("new/", views.bounty_create_view, name="create"),
    path("<int:pk>/edit/", views.bounty_edit_view, name="edit"),
    path("<int:pk>/claim/", views.bounty_claim_view, name="claim"),
    path("<int:pk>/done/", views.bounty_done_view, name="done"),
    path("<int:pk>/cancel/", views.bounty_cancel_view, name="cancel"),
    path("<int:pk>/cancel-revoked/", views.bounty_cancel_revoked_view, name="cancel_revoked"),
    path("<int:pk>/report/", views.bounty_report_view, name="report"),
    path("<int:pk>/toggle-hide/", views.bounty_toggle_hide_view, name="toggle_hide"),
    path("reports/<int:pk>/resolve/", views.report_resolve_view, name="resolve_report"),
    path("mod/", views.mod_queue_view, name="mod_queue"),
    # Slug pattern at the end so specific sub-paths match first
    path("<int:pk>/<slug:slug>/", views.bounty_detail_view, name="detail"),
]
