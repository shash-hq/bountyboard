from django.contrib.auth.models import AbstractUser, UserManager
from django.db import models
from django.db.models.functions import Lower
from django.utils import timezone


class BountyUserManager(UserManager):
    """Normalizes email (trim + lower) so the stored form is canonical.

    ``bulk_create`` and raw SQL bypass the manager, which is why identity uniqueness is also
    enforced in the database (see ``User.Meta.constraints``), not just here.
    """

    def _create_user(self, username, email, password, **extra_fields):
        email = (email or "").strip().lower()
        return super()._create_user(username, email, password, **extra_fields)

    def create_superuser(self, username=None, email=None, password=None, **extra_fields):
        # Admins must be able to log in once login requires a verified email.
        extra_fields.setdefault("email_verified_at", timezone.now())
        return super().create_superuser(username, email, password, **extra_fields)


class User(AbstractUser):
    # Required (AbstractUser makes it blank=True).
    email = models.EmailField("email address")
    email_verified_at = models.DateTimeField(null=True, blank=True)
    # Stateful email-verification token: only the sha256 of the emailed value is stored, so a
    # DB leak yields no usable links. One live token per user: a resend overwrites it, and a
    # successful confirm nulls it (single-use). See apps/accounts/services.py.
    email_token_hash = models.CharField(max_length=64, null=True, blank=True, editable=False)
    email_token_expires_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects = BountyUserManager()

    class Meta(AbstractUser.Meta):
        # Case-insensitive identity, enforced by the DB. The plain unique=True on username
        # stays (exact match); these close the "Arjun" vs "arjun" / "A@x.com" vs "a@x.com" gap.
        constraints = [
            models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_uniq"),
            models.UniqueConstraint(Lower("username"), name="accounts_user_username_ci_uniq"),
        ]
        permissions = [
            # Held only by the Verifiers group (least privilege): lets a desk volunteer mark a
            # student verified. Used from PR 4 on.
            ("verify_student", "Can verify students at a verification desk"),
        ]
