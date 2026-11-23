from django import forms
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

from .services import username_available

User = get_user_model()


def email_domain_allowed(email: str, allowed_domain: str) -> bool:
    """True if the email's domain is exactly ``allowed_domain`` or a subdomain of it.

    A bare ``endswith(domain)`` is wrong: "notiitb.ac.in" ends with "iitb.ac.in".
    Requiring the "." boundary means only "iitb.ac.in" and "x.iitb.ac.in" pass.
    Both sides are stripped and lowercased (DNS names are case-insensitive). A leading "@" or
    "." on the configured value (".iitb.ac.in" is a common way to say "this domain and its
    subdomains") is ignored.
    """
    allowed = allowed_domain.strip().lower().lstrip("@.")
    domain = email.strip().lower().rpartition("@")[2]
    if not allowed or not domain:
        return False
    return domain == allowed or domain.endswith("." + allowed)


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
        email = self.cleaned_data["email"].strip().lower()

        # Optional ALLOWED_EMAIL_DOMAIN restriction
        allowed_domain = getattr(settings, "ALLOWED_EMAIL_DOMAIN", "").strip()
        if allowed_domain:
            if not email_domain_allowed(email, allowed_domain):
                raise ValidationError(
                    f"Only email addresses ending with '@{allowed_domain}' are permitted on this campus board."
                )
        return email

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
