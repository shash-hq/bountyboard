from django import forms
from django.conf import settings
from django.contrib.auth import authenticate, get_user_model
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError

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
        username = self.cleaned_data["username"].strip()
        if User.objects.filter(username__iexact=username).exists():
            raise ValidationError("This username is already taken. Pick a different handle.")
        return username

    def clean_email(self):
        email = self.cleaned_data["email"].strip().lower()
        if User.objects.filter(email__iexact=email).exists():
            raise ValidationError("An account with this email already exists.")

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
        p1 = cleaned_data.get("password")
        p2 = cleaned_data.get("password_confirm")

        if p1 and p2:
            if p1 != p2:
                self.add_error("password_confirm", "Passwords do not match.")
            else:
                validate_password(p1)
        return cleaned_data


class LoginForm(forms.Form):
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
            cleaned_data["user"] = user

        return cleaned_data
