from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import User


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
        return [*super().get_readonly_fields(request, obj), "email", "email_verified_at"]

    fieldsets = [
        *DjangoUserAdmin.fieldsets,
        ("Email verification", {"fields": ["email_verified_at"]}),
    ]
    add_fieldsets = [
        (None, {"classes": ["wide"], "fields": ["username", "email", "password1", "password2"]}),
    ]
