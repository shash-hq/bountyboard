from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import AuditEvent, CampusVerification, IdentityReview, StudentIdentity, User


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    list_display = ["username", "email", "email_verified_at", "is_staff", "is_active"]
    search_fields = ["username", "email", "first_name", "last_name"]
    # Email changes must go through a service that also resets verification (a later PR),
    # so the admin shows it but cannot edit it.
    def get_readonly_fields(self, request, obj=None):
        # On the add form the email must be settable; afterwards it is read-only.
        if obj is None:
            return super().get_readonly_fields(request, obj)
        return [
            *super().get_readonly_fields(request, obj),
            "email", "email_verified_at", "staff_since", "staff_source", "staff_granted_by", "deleted_at",
        ]

    fieldsets = [
        *DjangoUserAdmin.fieldsets,
        ("Email verification", {"fields": ["email_verified_at", "deleted_at"]}),
        # Shown, never edited: the staff role changes only through services (moderator grant or
        # the email allowlist), each of which writes an AuditEvent.
        ("Staff role (read-only)", {"fields": ["staff_since", "staff_source", "staff_granted_by"]}),
    ]
    add_fieldsets = [
        (None, {"classes": ["wide"], "fields": ["username", "email", "password1", "password2"]}),
    ]


class ReadOnlyAdmin(admin.ModelAdmin):
    """View only. State changes go through services (locks, transitions, an AuditEvent), never the
    admin form, so there is no add, change, delete or bulk action here."""

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_actions(self, request):
        return {}  # also removes "delete selected"


@admin.register(CampusVerification)
class CampusVerificationAdmin(ReadOnlyAdmin):
    # card_name_enc is deliberately absent from every list below, so neither the changelist nor the
    # detail page ever loads or renders it (the default manager defers it as well).
    list_display = ["id", "user", "method", "verified_at", "verified_by", "revoked_at", "revoke_reason"]
    list_filter = ["method", "revoke_reason"]
    search_fields = ["user__username"]
    fields = [
        "user", "method", "verified_at", "verified_by", "verified_email", "identity", "revoked_at",
        "revoked_by", "revoke_reason", "revoke_note", "renewal_reminder_sent_at",
    ]
    list_select_related = ["user", "verified_by"]


@admin.register(StudentIdentity)
class StudentIdentityAdmin(ReadOnlyAdmin):
    # The HMAC is not shown: moderators identify a binding by user and last 4 digits.
    list_display = ["id", "user", "reg_no_last4", "source", "bound_at"]
    list_filter = ["source"]
    search_fields = ["user__username"]
    fields = ["user", "reg_no_last4", "source", "bound_at"]
    list_select_related = ["user"]


@admin.register(IdentityReview)
class IdentityReviewAdmin(ReadOnlyAdmin):
    list_display = ["id", "claimant", "status", "resolution", "claimant_source", "created_at"]
    list_filter = ["status", "resolution"]
    search_fields = ["claimant__username"]
    fields = [
        "claimant", "existing_identity", "claimed_last4", "claimant_source", "status", "resolution",
        "resolved_by", "resolved_at", "note", "created_at",
    ]
    list_select_related = ["claimant"]


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    """Append-only trail: nothing here can edit or delete a row."""

    list_display = ["id", "created_at", "action", "actor", "subject", "reason"]
    list_filter = ["action"]
    search_fields = ["actor__username", "subject__username"]
    fields = ["created_at", "action", "actor", "subject", "reason", "note", "verification"]
    list_select_related = ["actor", "subject"]
