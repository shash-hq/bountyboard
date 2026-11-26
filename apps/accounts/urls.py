from django.contrib.auth import views as auth_views
from django.contrib.auth.decorators import login_not_required
from django.urls import path, reverse_lazy
from . import mod_views, views

app_name = "accounts"

urlpatterns = [
    path("signup/", views.signup_view, name="signup"),
    path("login/", views.login_view, name="login"),
    path("logout/", views.logout_view, name="logout"),
    path("desk/", views.desk_view, name="desk"),
    path("mod/", mod_views.mod_index_view, name="mod_index"),
    path("mod/users/<int:pk>/", mod_views.mod_user_view, name="mod_user"),
    path("mod/users/<int:pk>/reveal-contact/", mod_views.mod_reveal_contact_view, name="mod_reveal_contact"),
    path("mod/users/<int:pk>/reveal-card-name/", mod_views.mod_reveal_card_name_view, name="mod_reveal_card_name"),
    path("mod/users/<int:pk>/revoke/", mod_views.mod_revoke_view, name="mod_revoke"),
    path("mod/users/<int:pk>/clear-block/", mod_views.mod_clear_block_view, name="mod_clear_block"),
    path("mod/users/<int:pk>/release-identity/", mod_views.mod_release_identity_view, name="mod_release_identity"),
    path("mod/reviews/", mod_views.mod_reviews_view, name="mod_reviews"),
    path("mod/reviews/<int:pk>/resolve/", mod_views.mod_resolve_review_view, name="mod_resolve_review"),
    path("mod/users/<int:pk>/grant-staff/", mod_views.mod_grant_staff_view, name="mod_grant_staff"),
    path("mod/users/<int:pk>/revoke-staff/", mod_views.mod_revoke_staff_view, name="mod_revoke_staff"),
    path("renew/", views.renew_view, name="renew"),
    path("renew/<int:uid>/<slug:token>/", views.renew_confirm_view, name="renew_confirm"),
    path("notifications/read/", views.notifications_read_view, name="notifications_read"),
    path("profile/contact/", views.contact_handle_view, name="contact_handle"),
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
