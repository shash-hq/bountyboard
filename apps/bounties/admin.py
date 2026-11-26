from django.contrib import admin, messages
from .models import Bounty, Claim, Report


@admin.action(description="Hide selected bounties from public board")
def hide_bounties(modeladmin, request, queryset):
    count = queryset.update(is_hidden=True)
    messages.success(request, f"{count} bounties hidden.")


@admin.action(description="Unhide selected bounties")
def unhide_bounties(modeladmin, request, queryset):
    count = queryset.update(is_hidden=False)
    messages.success(request, f"{count} bounties made visible.")


class ServiceOwnedAdmin(admin.ModelAdmin):
    """Admin for rows whose state only the services may write (invariant I5).

    - `service_fields` are read-only.
    - No "Add" page: creating here would skip the service's checks (open-bounty cap, claimable bounty).
    - A change writes only the edited columns. A full-row save would write back the status read when
      the request started, silently undoing a transition that committed in between.
    """

    service_fields: tuple = ()

    def get_readonly_fields(self, request, obj=None):
        return [*super().get_readonly_fields(request, obj), *self.service_fields]

    def has_add_permission(self, request):
        return False

    def save_model(self, request, obj, form, change):
        concrete = {f.name for f in obj._meta.concrete_fields}
        obj.save(update_fields=[*(n for n in form.changed_data if n in concrete), "updated_at"])


@admin.register(Bounty)
class BountyAdmin(ServiceOwnedAdmin):
    # deadline too: moving an expired bounty's deadline would make it live again past the cap (I4).
    service_fields = ("status", "accepted_claim", "deadline")
    list_display = [
        "title",
        "poster",
        "category",
        "reward",
        "status",
        "deadline",
        "is_hidden",
        "created_at",
    ]
    list_filter = ["status", "category", "is_hidden", "created_at"]
    search_fields = ["title", "description", "poster__username", "poster__email"]
    prepopulated_fields = {"slug": ("title",)}
    actions = [hide_bounties, unhide_bounties]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"


@admin.register(Claim)
class ClaimAdmin(ServiceOwnedAdmin):
    # bounty and claimant too: moving an accepted claim would break I2, and re-pointing one would break I8.
    service_fields = ("status", "bounty", "claimant")
    list_display = ["id", "bounty", "claimant", "status", "created_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["claimant__username", "bounty__title", "message"]
    readonly_fields = ["created_at", "updated_at"]


@admin.action(description="Mark selected reports as RESOLVED")
def mark_reports_resolved(modeladmin, request, queryset):
    count = queryset.update(resolved=True)
    messages.success(request, f"{count} reports marked as resolved.")


@admin.action(description="Resolve and hide reported bounties")
def resolve_and_hide_bounty(modeladmin, request, queryset):
    bounties_to_hide = [r.bounty_id for r in queryset]
    Bounty.objects.filter(id__in=bounties_to_hide).update(is_hidden=True)
    queryset.update(resolved=True)
    messages.success(request, "Reports resolved and reported bounties hidden.")


@admin.register(Report)
class ReportAdmin(admin.ModelAdmin):
    list_display = ["id", "bounty", "reporter", "reason", "resolved", "created_at"]
    list_filter = ["resolved", "reason", "created_at"]
    search_fields = ["bounty__title", "reporter__username", "note"]
    actions = [mark_reports_resolved, resolve_and_hide_bounty]
