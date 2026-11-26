from django.conf import settings
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


class StaffSource(models.TextChoices):
    MODERATOR = "MODERATOR", "Granted by a moderator"
    ALLOWLIST = "ALLOWLIST", "STAFF_EMAIL_ALLOWLIST match"


class User(AbstractUser):
    # Required (AbstractUser makes it blank=True).
    email = models.EmailField("email address")
    email_verified_at = models.DateTimeField(null=True, blank=True)
    # Stateful email-verification token: only the sha256 of the emailed value is stored, so a
    # DB leak yields no usable links. One live token per user: a resend overwrites it, and a
    # successful confirm nulls it (single-use). See apps/accounts/services.py.
    email_token_hash = models.CharField(max_length=64, null=True, blank=True, editable=False)
    email_token_expires_at = models.DateTimeField(null=True, blank=True, editable=False)

    # Staff is a role, orthogonal to campus verification. It is granted only by a moderator or by
    # STAFF_EMAIL_ALLOWLIST, never inferred from the email domain (students hold @lpu.co.in too).
    staff_since = models.DateTimeField(null=True, blank=True, editable=False)
    staff_source = models.CharField(
        max_length=16, choices=StaffSource.choices, null=True, blank=True, editable=False
    )
    staff_granted_by = models.ForeignKey(
        "self", null=True, blank=True, editable=False, on_delete=models.SET_NULL,
        related_name="+",
    )

    # Optional WhatsApp number or handle, shown only to the other party once a claim between you is
    # ACCEPTED (policies.can_view_contact). Email is always shown at that point; this is the extra.
    contact_handle = models.CharField(max_length=100, blank=True, default="")
    # Set when the owner deletes their account. The row is kept (anonymized, inactive) so the
    # registration-number binding, the revocation history and the counterparties' bounties and
    # claims all stay intact. See apps/accounts/deletion.py.
    deleted_at = models.DateTimeField(null=True, blank=True, editable=False)

    objects = BountyUserManager()

    class Meta(AbstractUser.Meta):
        # Case-insensitive identity, enforced by the DB. The plain unique=True on username
        # stays (exact match); these close the "Arjun" vs "arjun" / "A@x.com" vs "a@x.com" gap.
        constraints = [
            models.UniqueConstraint(Lower("email"), name="accounts_user_email_ci_uniq"),
            models.UniqueConstraint(Lower("username"), name="accounts_user_username_ci_uniq"),
            # A staff grant always records when and how, and a user with neither is not staff.
            models.CheckConstraint(
                condition=(
                    models.Q(staff_source__isnull=True, staff_since__isnull=True)
                    | models.Q(staff_source__isnull=False, staff_since__isnull=False)
                ),
                name="accounts_user_staff_source_iff_since",
            ),
            # A deleted account can never be active, whatever code path flips is_active.
            models.CheckConstraint(
                condition=models.Q(deleted_at__isnull=True) | models.Q(is_active=False),
                name="accounts_user_deleted_is_inactive",
            ),
        ]
        permissions = [
            # Held only by the Verifiers group (least privilege): lets a desk volunteer mark a
            # student verified. Used from PR 4 on.
            ("verify_student", "Can verify students at a verification desk"),
        ]


class IdentitySource(models.TextChoices):
    SELF_DECLARED = "SELF_DECLARED", "Typed in on the campus-email path"
    CARD_CHECKED = "CARD_CHECKED", "A verifier saw the physical ID card"


class StudentIdentity(models.Model):
    """The permanent registration-number binding, for Sybil resistance.

    Only ``HMAC-SHA256(REG_NO_HMAC_KEY, normalized_reg_no)`` and the last 4 digits are stored,
    never the number itself. The binding outlives revocation (a revoked user cannot re-verify the
    same card on a new account); only an audited moderator ``release_identity`` frees it.
    """

    reg_no_hmac = models.CharField(max_length=64, unique=True)
    reg_no_last4 = models.CharField(max_length=4)
    # One binding per user: re-verification reuses it instead of adding a second number.
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="student_identity"
    )
    source = models.CharField(max_length=16, choices=IdentitySource.choices)
    bound_at = models.DateTimeField(default=timezone.now)

    def __str__(self):
        return f"identity of user {self.user_id} (…{self.reg_no_last4})"


class VerificationMethod(models.TextChoices):
    # VOUCH is deliberately absent until it is designed (adding it later is an ordinary migration).
    CAMPUS_EMAIL = "CAMPUS_EMAIL", "Campus email link"
    IN_PERSON = "IN_PERSON", "In-person ID card check"


class RevokeReason(models.TextChoices):
    LEFT_CAMPUS = "LEFT_CAMPUS", "Left campus"
    VERIFICATION_INVALID = "VERIFICATION_INVALID", "Verification was invalid"
    POLICY_VIOLATION = "POLICY_VIOLATION", "Policy violation"
    # System-only reasons:
    EMAIL_CHANGED = "EMAIL_CHANGED", "Email changed"
    EXPIRED = "EXPIRED", "Lapsed past the TTL (set by the sweeper)"
    SUPERSEDED = "SUPERSEDED", "Closed by a renewal"
    ACCOUNT_DELETED = "ACCOUNT_DELETED", "The owner deleted their account"


class CampusVerificationManager(models.Manager):
    def get_queryset(self):
        # The card name is the most sensitive column here. Deferring it means it is never loaded
        # (so never rendered or logged) unless code asks for it by name, which only
        # services.reveal_card_name() will do.
        return super().get_queryset().defer("card_name_enc")


class CampusVerification(models.Model):
    """One row per verification event.

    Whether a user is *currently* campus-verified is decided by the policy from
    ``revoked_at IS NULL AND verified_at > now - VERIFICATION_TTL_DAYS``, not by a sweeper, so a
    missed tick cannot extend access. The sweeper only closes lapsed rows (``EXPIRED``) so that
    the one-active-row partial unique index stays true and a renewal can insert a fresh row.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="campus_verifications"
    )
    method = models.CharField(max_length=16, choices=VerificationMethod.choices)
    verified_at = models.DateTimeField(default=timezone.now)
    # PROTECT: IN_PERSON requires a verifier (check below), so a verifier with history cannot be
    # deleted out from under it (SET_NULL would violate the constraint).
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.PROTECT,
        related_name="+",
    )
    verified_email = models.EmailField(null=True, blank=True)
    # NULL when the registration number collided with another user's binding (verification still
    # succeeds; an IdentityReview is opened instead).
    identity = models.ForeignKey(
        StudentIdentity, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # Fernet ciphertext (MultiFernet over CARD_NAME_KEYS) of the name printed on the card.
    # IN_PERSON only. Never in a form, template context or __str__.
    card_name_enc = models.TextField(null=True, blank=True, editable=False)
    revoked_at = models.DateTimeField(null=True, blank=True)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    revoke_reason = models.CharField(
        max_length=24, choices=RevokeReason.choices, null=True, blank=True
    )
    revoke_note = models.TextField(blank=True, default="")
    renewal_reminder_sent_at = models.DateTimeField(null=True, blank=True)
    # A row revoked as invalid or for a policy violation blocks re-verification until a moderator
    # sets these (see campus.is_blocked). The revocation itself stays on record.
    block_cleared_at = models.DateTimeField(null=True, blank=True)
    block_cleared_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )

    objects = CampusVerificationManager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["user"],
                condition=models.Q(revoked_at__isnull=True),
                name="accounts_campusverification_one_active_per_user",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(revoked_at__isnull=True, revoke_reason__isnull=True)
                    | models.Q(revoked_at__isnull=False, revoke_reason__isnull=False)
                ),
                name="accounts_campusverification_revoked_iff_reason",
            ),
            models.CheckConstraint(
                # isnull=False is needed: "NULL IN (...)" is unknown, which a CHECK lets through.
                condition=(
                    models.Q(block_cleared_at__isnull=True)
                    | models.Q(
                        revoke_reason__isnull=False,
                        revoke_reason__in=["VERIFICATION_INVALID", "POLICY_VIOLATION"],
                    )
                ),
                name="accounts_campusverification_block_cleared_only_if_blocking",
            ),
            models.CheckConstraint(
                # An ACTIVE in-person row must carry a card name. A closed (revoked) row may have it
                # wiped: account deletion removes the name but keeps the row as revocation history.
                condition=(
                    ~models.Q(method="IN_PERSON")
                    | (
                        models.Q(verified_by__isnull=False)
                        & (models.Q(card_name_enc__isnull=False) | models.Q(revoked_at__isnull=False))
                    )
                ),
                name="accounts_campusverification_in_person_fields",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(method="CAMPUS_EMAIL")
                    | models.Q(
                        verified_email__isnull=False,
                        verified_by__isnull=True,
                        identity__isnull=True,
                    )
                ),
                name="accounts_campusverification_campus_email_fields",
            ),
        ]

    def __str__(self):
        return f"{self.method} verification of user {self.user_id} at {self.verified_at:%Y-%m-%d}"


class ReviewStatus(models.TextChoices):
    OPEN = "OPEN", "Open"
    RESOLVED = "RESOLVED", "Resolved"


class ReviewResolution(models.TextChoices):
    KEEP_EXISTING = "KEEP_EXISTING", "Keep the existing binding"
    REASSIGN = "REASSIGN", "Rebind to the newcomer, revoke the old holder"
    BOTH_LEGIT_TYPO = "BOTH_LEGIT_TYPO", "Newcomer mistyped; re-enter"


class IdentityReview(models.Model):
    """Moderator queue item opened when a registration number is already bound to someone else.

    A collision never blocks verification, so a mistyped or squatted number cannot lock out the
    real owner. It is flagged here instead.
    """

    claimant = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="identity_reviews"
    )
    existing_identity = models.ForeignKey(
        StudentIdentity, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    claimed_hmac = models.CharField(max_length=64)
    claimed_last4 = models.CharField(max_length=4)
    claimant_source = models.CharField(max_length=16, choices=IdentitySource.choices)
    status = models.CharField(
        max_length=8, choices=ReviewStatus.choices, default=ReviewStatus.OPEN
    )
    resolution = models.CharField(
        max_length=16, choices=ReviewResolution.choices, null=True, blank=True
    )
    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+",
    )
    resolved_at = models.DateTimeField(null=True, blank=True)
    note = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [
            # At most one open review per user.
            models.UniqueConstraint(
                fields=["claimant"],
                condition=models.Q(status="OPEN"),
                name="accounts_identityreview_one_open_per_claimant",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="OPEN", resolution__isnull=True, resolved_at__isnull=True)
                    | models.Q(
                        status="RESOLVED", resolution__isnull=False, resolved_at__isnull=False
                    )
                ),
                name="accounts_identityreview_resolved_iff_resolution",
            ),
        ]

    def __str__(self):
        return f"{self.status} review of user {self.claimant_id}"


class AuditAction(models.TextChoices):
    VERIFIED = "VERIFIED"
    REVOKED = "REVOKED"
    IDENTITY_RELEASED = "IDENTITY_RELEASED"
    STAFF_GRANTED = "STAFF_GRANTED"
    STAFF_REVOKED = "STAFF_REVOKED"
    CONTACT_VIEWED = "CONTACT_VIEWED"
    CARD_NAME_VIEWED = "CARD_NAME_VIEWED"
    REG_NO_LOOKUP = "REG_NO_LOOKUP"
    IDENTITY_REVIEW_RESOLVED = "IDENTITY_REVIEW_RESOLVED"
    ACCOUNT_DELETED = "ACCOUNT_DELETED"
    BLOCK_CLEARED = "BLOCK_CLEARED"


class AuditEvent(models.Model):
    """Append-only record of verification and privileged moderator actions.

    Covers "log every revocation" and "moderator access to contact or card name is audited".
    Rows survive deletion of either user (SET_NULL), so the trail outlives the accounts.
    """

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    subject = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="+"
    )
    action = models.CharField(max_length=24, choices=AuditAction.choices)
    reason = models.CharField(max_length=32, blank=True, default="")
    note = models.TextField(blank=True, default="")
    verification = models.ForeignKey(
        CampusVerification, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        indexes = [models.Index(fields=["subject", "-created_at"])]

    def __str__(self):
        return f"{self.action} by {self.actor_id} on {self.subject_id}"
