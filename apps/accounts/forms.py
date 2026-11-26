from django import forms
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .models import RevokeReason
from .services import username_available

User = get_user_model()


class SignupForm(forms.Form):
    username = forms.CharField(
        max_length=150,
        widget=forms.TextInput(attrs={
            "class": "input-text",
            "placeholder": "e.g. arjun_ece",
            "autocomplete": "username",
        }),
        help_text="Letters, numbers and underscores only (max 150 chars).",
    )
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            "class": "input-text",
            "placeholder": "your_id@campus.edu.in",
            "autocomplete": "email",
        }),
        help_text="Your campus or personal email.",
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            "class": "input-text",
            "placeholder": "Minimum 8 characters",
            "autocomplete": "new-password",
        }),
    )
    password_confirm = forms.CharField(
        label="Confirm Password",
        widget=forms.PasswordInput(attrs={
            "class": "input-text",
            "placeholder": "Re-type your password",
            "autocomplete": "new-password",
        }),
    )

    def clean_username(self):
        return self.cleaned_data["username"].strip()

    def clean_email(self):
        # No uniqueness check here on purpose: telling a stranger "this address already has an
        # account" is an enumeration oracle. services.signup() handles an address that is in use.
        # Signup is open to any domain. Being a campus student is established separately, at email
        # confirmation (campus.verify_campus_email) or in person; the domain gates nothing here.
        return self.cleaned_data["email"].strip().lower()

    def clean(self):
        cleaned_data = super().clean()
        username, email = cleaned_data.get("username"), cleaned_data.get("email")
        if username and email and not username_available(username, email):
            self.add_error("username", "This username is already taken. Pick a different handle.")
        p1 = cleaned_data.get("password")
        p2 = cleaned_data.get("password_confirm")

        if p1 and p2:
            if p1 != p2:
                self.add_error("password_confirm", "Passwords do not match.")
            else:
                validate_password(p1)
        return cleaned_data


class ConfirmEmailForm(forms.Form):
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            "class": "input-text",
            "placeholder": "The password you chose at signup",
            "autocomplete": "current-password",
        }),
    )

    def __init__(self, *args, ask_reg_no=False, **kwargs):
        super().__init__(*args, **kwargs)
        if ask_reg_no:
            self.fields["reg_no"] = RegNoField()


class RegNoField(forms.CharField):
    """Registration number, collected only on the campus-email path."""

    def __init__(self, **kwargs):
        super().__init__(
            label="Registration number",
            max_length=32,
            widget=forms.TextInput(attrs={
                "class": "input-text", "placeholder": "e.g. 12345678",
                "inputmode": "numeric", "autocomplete": "off",
            }),
            **kwargs,
        )


class ResendVerificationForm(forms.Form):
    email = forms.EmailField(
        widget=forms.EmailInput(attrs={
            "class": "input-text",
            "placeholder": "the email you signed up with",
            "autocomplete": "email",
        }),
    )


class LoginForm(forms.Form):
    unverified = False  # set by clean() so the template can offer a "resend link" action
    username_or_email = forms.CharField(
        label="Username or Email",
        widget=forms.TextInput(attrs={
            "class": "input-text",
            "placeholder": "Username or campus email",
            "autocomplete": "username",
        }),
    )
    password = forms.CharField(
        widget=forms.PasswordInput(attrs={
            "class": "input-text",
            "placeholder": "Your password",
            "autocomplete": "current-password",
        }),
    )

    def clean(self):
        cleaned_data = super().clean()
        login_input = cleaned_data.get("username_or_email", "").strip()
        password = cleaned_data.get("password", "")

        if login_input and password:
            user = authenticate(username=login_input, password=password)
            if not user:
                # Try finding user by email
                try:
                    user_obj = User.objects.get(email__iexact=login_input)
                    user = authenticate(username=user_obj.username, password=password)
                except (User.DoesNotExist, User.MultipleObjectsReturned):
                    user = None

            if not user:
                raise ValidationError("Invalid username/email or password.")
            if not user.is_active:
                raise ValidationError("This account has been deactivated.")
            # Checked only AFTER the password is right, so "unverified" is never revealed to
            # someone who does not know the password.
            if user.email_verified_at is None:
                self.unverified = True
                raise ValidationError("Confirm your email first: use the link we sent you.")
            cleaned_data["user"] = user

        return cleaned_data


class DeskVerifyForm(forms.Form):
    """What a verifier types at the desk. The card name is the name *as printed*, free text."""

    username = forms.CharField(
        label="Student's username", max_length=150,
        widget=forms.TextInput(attrs={"class": "input-text", "autocomplete": "off", "autocapitalize": "none"}),
    )
    code = forms.CharField(
        label="Presence code (shown on the student's phone)", max_length=6,
        widget=forms.TextInput(attrs={"class": "input-text", "inputmode": "numeric", "autocomplete": "off"}),
    )
    reg_no = RegNoField()
    card_name = forms.CharField(
        label="Name as printed on the card", max_length=100,
        widget=forms.TextInput(attrs={"class": "input-text", "autocomplete": "off"}),
    )


class ContactHandleForm(forms.ModelForm):
    """The one optional contact detail beyond email. Single line, no control characters."""

    class Meta:
        model = User
        fields = ["contact_handle"]
        widgets = {
            "contact_handle": forms.TextInput(attrs={
                "class": "input-text", "placeholder": "WhatsApp number or handle (optional)",
                "autocomplete": "tel", "maxlength": "100",
            }),
        }

    def clean_contact_handle(self):
        value = " ".join((self.cleaned_data.get("contact_handle") or "").split())
        if any(ord(ch) < 32 for ch in value):
            raise ValidationError("Contact handle can't contain control characters.")
        return value


class ModReasonForm(forms.Form):
    """A required, free-text justification. Stored on the AuditEvent, never shown to the subject."""

    reason = forms.CharField(
        label="Why do you need this?", max_length=500,
        widget=forms.TextInput(attrs={"class": "input-text", "autocomplete": "off"}),
    )


class RegNoLookupForm(ModReasonForm):
    reg_no = RegNoField()


class RevokeForm(forms.Form):
    reason_code = forms.ChoiceField(label="Reason", choices=[])
    note = forms.CharField(
        label="Note (optional)", max_length=500, required=False,
        widget=forms.TextInput(attrs={"class": "input-text", "autocomplete": "off"}),
    )

    def __init__(self, *args, **kwargs):
        from .moderation import MODERATOR_REASONS

        super().__init__(*args, **kwargs)
        labels = dict(RevokeReason.choices)
        self.fields["reason_code"].choices = [(r, labels[r]) for r in MODERATOR_REASONS]


class ReleaseIdentityForm(forms.Form):
    note = forms.CharField(
        label="Why is the identity being released?", max_length=500,
        widget=forms.TextInput(attrs={"class": "input-text", "autocomplete": "off"}),
    )


class ClearBlockForm(forms.Form):
    note = forms.CharField(
        label="Why can they verify again?", max_length=500,
        widget=forms.TextInput(attrs={"class": "input-text", "autocomplete": "off"}),
    )


class ResolveReviewForm(forms.Form):
    resolution = forms.ChoiceField(label="Decision", choices=[])
    note = forms.CharField(
        label="Why?", max_length=500,
        widget=forms.TextInput(attrs={"class": "input-text", "autocomplete": "off"}),
    )

    def __init__(self, *args, **kwargs):
        from .models import ReviewResolution

        super().__init__(*args, **kwargs)
        self.fields["resolution"].choices = [
            c for c in ReviewResolution.choices if c[0] != ReviewResolution.BOTH_LEGIT_TYPO
        ]


class StaffNoteForm(forms.Form):
    note = forms.CharField(
        label="Note (optional)", max_length=500, required=False,
        widget=forms.TextInput(attrs={"class": "input-text", "autocomplete": "off"}),
    )


class RenewForm(forms.Form):
    """Confirming a renewal. The registration number is asked only if the account has no binding."""

    def __init__(self, *args, ask_reg_no=False, **kwargs):
        super().__init__(*args, **kwargs)
        if ask_reg_no:
            self.fields["reg_no"] = RegNoField()
