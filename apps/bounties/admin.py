from django.contrib import admin, messages
from .models import Bounty, BountyStatus, Claim, Report


@admin.action(description="Hide selected bounties from public board")
def hide_bounties(modeladmin, request, queryset):
    count = queryset.update(is_hidden=True)
    messages.success(request, f"{count} bounties hidden.")


@admin.action(description="Unhide selected bounties")
def unhide_bounties(modeladmin, request, queryset):
    count = queryset.update(is_hidden=False)
    messages.success(request, f"{count} bounties made visible.")


@admin.action(description="Mark selected bounties as DONE")
def mark_bounties_done(modeladmin, request, queryset):
    count = queryset.update(status=BountyStatus.DONE)
    messages.success(request, f"{count} bounties marked as DONE.")


@admin.register(Bounty)
class BountyAdmin(admin.ModelAdmin):
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
    actions = [hide_bounties, unhide_bounties, mark_bounties_done]
    readonly_fields = ["created_at", "updated_at"]
    date_hierarchy = "created_at"


@admin.register(Claim)
class ClaimAdmin(admin.ModelAdmin):
    list_display = ["id", "bounty", "claimant", "status", "contact", "created_at"]
    list_filter = ["status", "created_at"]
    search_fields = ["claimant__username", "bounty__title", "message", "contact"]
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
