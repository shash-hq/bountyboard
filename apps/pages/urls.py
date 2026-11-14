from django.urls import path
from . import views

app_name = "pages"

urlpatterns = [
    path("about/", views.about, name="about"),
    path("terms/", views.terms, name="terms"),
    path("privacy/", views.privacy, name="privacy"),
    path("guidelines/", views.guidelines, name="guidelines"),
    path("styleguide/", views.styleguide, name="styleguide"),
    path("layout/", views.set_layout, name="set_layout"),
    path("healthz/", views.healthz, name="healthz"),
    path("robots.txt", views.robots_txt, name="robots_txt"),
]
