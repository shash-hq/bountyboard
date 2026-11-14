from django.contrib.sitemaps import Sitemap
from django.urls import reverse
from apps.bounties.models import Bounty


class StaticViewSitemap(Sitemap):
    priority = 0.8
    changefreq = "daily"

    def items(self):
        return ["board", "pages:about", "pages:terms", "pages:privacy", "pages:guidelines"]

    def location(self, item):
        return reverse(item)


class BountySitemap(Sitemap):
    priority = 0.9
    changefreq = "hourly"

    def items(self):
        return Bounty.objects.filter(is_hidden=False).order_by("-created_at")

    def lastmod(self, obj):
        return obj.updated_at
