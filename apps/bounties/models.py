from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinLengthValidator, MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils import timezone
from django.utils.text import slugify

User = get_user_model()


class Category(models.TextChoices):
    TECH = "Tech", "Tech"
    DESIGN = "Design", "Design"
    WRITING = "Writing", "Writing"
    ACADEMICS = "Academics", "Academics"
    ERRANDS = "Errands", "Errands"
    OTHER = "Other", "Other"


class BountyStatus(models.TextChoices):
    OPEN = "OPEN", "OPEN"
    CLAIMED = "CLAIMED", "CLAIMED"
    DONE = "DONE", "DONE"
    CANCELLED = "CANCELLED", "CANCELLED"
    EXPIRED = "EXPIRED", "EXPIRED"  # set by services.expire_bounty (the sweeper); terminal


class ClaimStatus(models.TextChoices):
    PENDING = "PENDING", "PENDING"
    ACCEPTED = "ACCEPTED", "ACCEPTED"
    REJECTED = "REJECTED", "REJECTED"
    WITHDRAWN = "WITHDRAWN", "WITHDRAWN"
    CANCELLED = "CANCELLED", "CANCELLED"  # an ACCEPTED claim ended by the counterparty-revocation cancel


class ReportReason(models.TextChoices):
    ACADEMIC_DISHONESTY = "ACADEMIC_DISHONESTY", "Academic Dishonesty"
    SCAM = "SCAM", "Scam / Fake Bounty"
    HARASSMENT = "HARASSMENT", "Harassment / Offensive"
    COMMERCIAL_SPAM = "COMMERCIAL_SPAM", "Commercial Spam"
    OTHER = "OTHER", "Other"


class Bounty(models.Model):
    poster = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="posted_bounties"
    )
    title = models.CharField(
        max_length=80,
        validators=[MinLengthValidator(8, "Title must be at least 8 characters.")],
        help_text="Short, descriptive title (8-80 characters).",
    )
    slug = models.SlugField(max_length=100, blank=True)
    description = models.TextField(
        validators=[MinLengthValidator(20, "Description must be at least 20 characters.")],
        help_text="Clear scope and deliverables (20-1000 characters).",
    )
    category = models.CharField(
        max_length=20,
        choices=Category.choices,
        default=Category.TECH,
    )
    reward = models.PositiveIntegerField(
        validators=[
            MinValueValidator(10, "Minimum reward is ₹10."),
            MaxValueValidator(10000, "Maximum reward is ₹10,000."),
        ],
        help_text="Reward amount in INR (₹10 to ₹10,000).",
    )
    deadline = models.DateTimeField(
        help_text="When must this task be completed?",
    )
    status = models.CharField(
        max_length=15,
        choices=BountyStatus.choices,
        default=BountyStatus.OPEN,
    )
    accepted_claim = models.OneToOneField(
        "Claim",
        # RESTRICT, not SET_NULL: nulling the pointer would break I2 on a CLAIMED or DONE bounty.
        # Deleting the bounty itself (claim cascades with it) is still allowed.
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="accepted_for_bounty",
    )
    is_hidden = models.BooleanField(
        default=False,
        help_text="Hidden by moderators due to policy violations.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "Bounty"
        verbose_name_plural = "Bounties"
        permissions = [
            ("moderate", "Can moderate bounties and reports"),
        ]
        constraints = [
            # I2, the part one row can express. That the claim is ACCEPTED and belongs to this
            # bounty spans two tables, so the services stay responsible for it.
            models.CheckConstraint(
                condition=(
                    models.Q(status__in=[BountyStatus.CLAIMED, BountyStatus.DONE], accepted_claim__isnull=False)
                    | models.Q(
                        status__in=[BountyStatus.OPEN, BountyStatus.CANCELLED, BountyStatus.EXPIRED],
                        accepted_claim__isnull=True,
                    )
                ),
                name="accepted_claim_iff_claimed_or_done",
            ),
        ]

    def __str__(self):
        return f"{self.title} (₹{self.reward})"

    def clean(self):
        super().clean()
        # Max length check for description (20-1000)
        if self.description and len(self.description) > 1000:
            raise ValidationError({"description": "Description cannot exceed 1000 characters."})

        # Deadline validation for new OPEN bounties
        if self.deadline and self.pk is None and self.status == BountyStatus.OPEN and self.deadline <= timezone.now():
            raise ValidationError({"deadline": "Deadline must be in the future."})

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.title)[:80] or "bounty"
            self.slug = base_slug
        self.full_clean()
        super().save(*args, **kwargs)

    def get_absolute_url(self) -> str:
        return reverse("bounties:detail", kwargs={"pk": self.pk, "slug": self.slug})

    @property
    def is_expired(self) -> bool:
        """Expired: closed by the sweeper (EXPIRED), or past its deadline and not yet swept.

        The second clause is what makes expiry correct on a sleeping instance: it is time-based, so a
        missed tick can never keep an overdue bounty claimable.
        """
        return self.status == BountyStatus.EXPIRED or (
            self.status == BountyStatus.OPEN and self.deadline <= timezone.now()
        )

    @property
    def display_status(self) -> str:
        if self.is_expired:
            return "EXPIRED"
        return self.status

    def can_edit(self, user) -> bool:
        return user.is_authenticated and self.poster_id == user.id and self.status == BountyStatus.OPEN

    def can_claim(self, user) -> bool:
        if not user.is_authenticated:
            return False
        if self.poster_id == user.id:
            return False
        if self.status != BountyStatus.OPEN or self.is_expired or self.is_hidden:
            return False
        return not self.claims.filter(claimant=user).exists()


class Claim(models.Model):
    bounty = models.ForeignKey(
        Bounty, on_delete=models.CASCADE, related_name="claims"
    )
    claimant = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="submitted_claims"
    )
    message = models.TextField(
        validators=[MinLengthValidator(10, "Message must be at least 10 characters.")],
        help_text="Why you can solve this task (10-500 characters).",
    )
    status = models.CharField(
        max_length=15,
        choices=ClaimStatus.choices,
        default=ClaimStatus.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["bounty", "claimant"], name="unique_bounty_claimant"
            ),
            # I1
            models.UniqueConstraint(
                fields=["bounty"],
                condition=models.Q(status=ClaimStatus.ACCEPTED),
                name="one_accepted_claim_per_bounty",
            ),
        ]

    def __str__(self):
        return f"Claim by {self.claimant.username} on {self.bounty.title} [{self.status}]"

    def clean(self):
        super().clean()
        if self.message and len(self.message) > 500:
            raise ValidationError({"message": "Message cannot exceed 500 characters."})

        # Cannot claim own bounty
        if self.bounty_id and self.claimant_id and self.bounty.poster_id == self.claimant_id:
            raise ValidationError("You cannot claim your own bounty.")

        # Cannot claim non-open or expired bounty
        if self.pk is None and self.bounty_id:
            if self.bounty.status != BountyStatus.OPEN or self.bounty.is_expired or self.bounty.is_hidden:
                raise ValidationError("This bounty is no longer accepting claims.")

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)


class Report(models.Model):
    bounty = models.ForeignKey(
        Bounty, on_delete=models.CASCADE, related_name="reports"
    )
    reporter = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="filed_reports"
    )
    reason = models.CharField(
        max_length=30,
        choices=ReportReason.choices,
        default=ReportReason.OTHER,
    )
    note = models.TextField(
        blank=True,
        help_text="Optional context for campus moderators.",
    )
    resolved = models.BooleanField(
        default=False,
        help_text="Marked resolved by a moderator.",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Report on {self.bounty.title} by {self.reporter.username}"


class NotificationKind(models.TextChoices):
    CLAIM_RECEIVED = "CLAIM_RECEIVED", "A claim was submitted on your bounty"
    CLAIM_ACCEPTED = "CLAIM_ACCEPTED", "Your claim was accepted"
    CLAIM_REJECTED = "CLAIM_REJECTED", "Your claim was rejected"
    CLAIM_WITHDRAWN = "CLAIM_WITHDRAWN", "A claim on your bounty was withdrawn"
    BOUNTY_DONE = "BOUNTY_DONE", "The bounty you worked on was marked done"
    BOUNTY_CANCELLED = "BOUNTY_CANCELLED", "A bounty you claimed was cancelled"
    BOUNTY_EXPIRED = "BOUNTY_EXPIRED", "A bounty expired"
    RENEWAL_REMINDER = "RENEWAL_REMINDER", "Your campus verification is about to lapse"
    COUNTERPARTY_REVOKED = "COUNTERPARTY_REVOKED", "Counterparty lost campus verification"
    COUNTERPARTY_CANCELLED = "COUNTERPARTY_CANCELLED", "Counterparty cancelled after a verification revocation"


class EmailStatus(models.TextChoices):
    NONE = "NONE", "In-app only"
    PENDING = "PENDING", "Waiting to be emailed (or retried)"
    SENT = "SENT", "Emailed"
    DEAD = "DEAD", "Gave up after repeated failures"


class Notification(models.Model):
    """The transactional outbox, and the in-app inbox: one row per (event, recipient).

    `idempotency_key` is unique, so a retried transition or a double submit cannot notify twice.
    Rows are written inside the same transaction as the state change they describe (I7), which is
    why creation lives in `inbox.notify` and callers must already be in `transaction.atomic()`.

    Email is a scarce resource, so only some kinds ask for it (`email_status=PENDING`); the rest are
    in-app only (`NONE`). `outbox.py` delivers PENDING rows: once on commit for latency, and from
    `/internal/tick` with `SELECT ... FOR UPDATE SKIP LOCKED`, exponential backoff and a retry limit.
    Delivery is at-least-once (a crash between SMTP accepting a message and our commit can resend it).
    """

    recipient = models.ForeignKey(User, on_delete=models.CASCADE, related_name="notifications")
    kind = models.CharField(max_length=32, choices=NotificationKind.choices)
    bounty = models.ForeignKey(
        Bounty, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    # The claim an email is about. The email body is rendered from fresh rows at send time.
    claim = models.ForeignKey(
        Claim, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    message = models.CharField(max_length=300)
    idempotency_key = models.CharField(max_length=120, unique=True)
    created_at = models.DateTimeField(default=timezone.now)
    read_at = models.DateTimeField(null=True, blank=True)

    email_status = models.CharField(max_length=8, choices=EmailStatus.choices, default=EmailStatus.NONE)
    attempts = models.PositiveSmallIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    last_error = models.CharField(max_length=200, blank=True, default="")

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["recipient", "read_at", "-created_at"]),
            models.Index(fields=["next_attempt_at"], condition=models.Q(email_status="PENDING"), name="notification_pending_due"),
            # The daily budget counts recent SENT rows on every delivery.
            models.Index(fields=["delivered_at"], condition=models.Q(email_status="SENT"), name="notification_sent_at"),
        ]
        constraints = [
            # A PENDING row always says when to try next, and only PENDING rows do.
            models.CheckConstraint(
                condition=(
                    models.Q(email_status="PENDING", next_attempt_at__isnull=False)
                    | (~models.Q(email_status="PENDING") & models.Q(next_attempt_at__isnull=True))
                ),
                name="notification_pending_iff_next_attempt",
            ),
            models.CheckConstraint(
                condition=models.Q(email_status="SENT", delivered_at__isnull=False) | ~models.Q(email_status="SENT"),
                name="notification_sent_has_delivered_at",
            ),
        ]

    def __str__(self):
        return f"{self.kind} for {self.recipient_id}"
