from django.conf import settings
from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("apps.accounts.urls", namespace="accounts")),
    path("bounties/", include("apps.bounties.urls", namespace="bounties")),
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
