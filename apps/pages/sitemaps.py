from django.contrib.sitemaps import Sitemap
from django.urls import reverse


class StaticViewSitemap(Sitemap):
    """Public pages only. The board and bounty pages need a login, so they are not listed."""

    priority = 0.8
    changefreq = "weekly"

    def items(self):
        return ["landing", "pages:about", "pages:terms", "pages:privacy", "pages:guidelines"]

    def location(self, item):
        return reverse(item)
