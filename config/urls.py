from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_not_required
from django.contrib.sitemaps.views import sitemap
from django.urls import include, path
from apps.accounts.views import me_view
from apps.bounties import views as bounty_views
from apps.pages import views as pages_views
from apps.pages.sitemaps import StaticViewSitemap

sitemaps = {
    "static": StaticViewSitemap,
}

urlpatterns = [
    path("admin/", admin.site.urls),
    path("", pages_views.landing, name="landing"),
    path("board/", bounty_views.board_view, name="board"),
    path("me/", me_view, name="me"),
    path("accounts/", include("apps.accounts.urls", namespace="accounts")),
    path("bounties/", include("apps.bounties.urls", namespace="bounties")),
    
    # POST-only claim lifecycle routes
    path("claims/<int:pk>/accept/", bounty_views.claim_accept_view, name="claim_accept"),
    path("claims/<int:pk>/reject/", bounty_views.claim_reject_view, name="claim_reject"),
    path("claims/<int:pk>/withdraw/", bounty_views.claim_withdraw_view, name="claim_withdraw"),
    
    path("sitemap.xml", login_not_required(sitemap), {"sitemaps": sitemaps}, name="sitemap"),
    path("", include("apps.pages.urls", namespace="pages")),
]

if "debug_toolbar" in settings.INSTALLED_APPS:
    import debug_toolbar
    urlpatterns = [
        path("__debug__/", include(debug_toolbar.urls)),
    ] + urlpatterns

handler400 = "apps.pages.views.handler400"
handler403 = "apps.pages.views.handler403"
handler404 = "apps.pages.views.handler404"
handler500 = "apps.pages.views.handler500"
