from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.urls import path, reverse_lazy
from . import views

app_name = "accounts"

urlpatterns = [
    path("signup/", views.signup_view, name="signup"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("verify-email/resend/", views.resend_verification_view, name="resend_verification"),
    path("verify-email/<int:uid>/<slug:token>/", views.verify_email_view, name="verify_email"),
    
    # Password Reset flow with custom neo-brutalist templates
    path(
        "password-reset/",
        login_not_required(views.RateLimitedPasswordResetView.as_view(
            template_name="accounts/password_reset.html",
            email_template_name="accounts/password_reset_email.txt",
            subject_template_name="accounts/password_reset_subject.txt",
            success_url=reverse_lazy("accounts:password_reset_done"),
        )),
        name="password_reset",
    ),
    path(
        "password-reset/done/",
        login_not_required(auth_views.PasswordResetDoneView.as_view(
            template_name="accounts/password_reset_done.html",
        )),
        name="password_reset_done",
    ),
    path(
        "password-reset/<uidb64>/<token>/",
        login_not_required(auth_views.PasswordResetConfirmView.as_view(
            template_name="accounts/password_reset_confirm.html",
            success_url=reverse_lazy("accounts:password_reset_complete"),
        )),
        name="password_reset_confirm",
    ),
    path(
        "password-reset/complete/",
        login_not_required(auth_views.PasswordResetCompleteView.as_view(
            template_name="accounts/password_reset_complete.html",
        )),
        name="password_reset_complete",
    ),
]
