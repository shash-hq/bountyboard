"""No third-party fonts or stylesheets: assets are self-hosted and the production build resolves them."""
import re
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.test import SimpleTestCase, override_settings

ROOT = Path(__file__).resolve().parent.parent
FOREIGN = re.compile(r"googleapis\.com|gstatic\.com|@import\s+url\(\s*['\"]?https?:", re.I)


class SelfHostedAssetsTests(SimpleTestCase):
    def test_no_css_or_template_references_a_third_party_font_host(self):
        offenders = []
        for pattern in ("static/css/*.css", "templates/**/*.html"):
            for path in ROOT.glob(pattern):
                if FOREIGN.search(path.read_text(encoding="utf-8", errors="ignore")):
                    offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [])

    def test_every_font_face_url_points_at_a_file_that_exists(self):
        css_path = ROOT / "static/css/tokens.css"
        urls = re.findall(r"url\(['\"]?([^)'\"]+)['\"]?\)", css_path.read_text())
        font_urls = [u for u in urls if u.endswith(".woff2")]
        self.assertGreaterEqual(len(font_urls), 2)
        for u in font_urls:
            with self.subTest(url=u):
                self.assertTrue((css_path.parent / u).resolve().is_file())

    def test_production_static_build_resolves_the_fonts(self):
        """collectstatic with the production manifest storage fails if a url() in the CSS is missing."""
        with tempfile.TemporaryDirectory() as out:
            storages = {
                "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
            }
            with override_settings(STATIC_ROOT=out, STORAGES=storages):
                call_command("collectstatic", interactive=False, verbosity=0)
            built = "".join(p.read_text() for p in Path(out, "css").glob("tokens.*.css"))
            self.assertRegex(built, r"archivo-latin\.[0-9a-f]{12}\.woff2")  # hashed, i.e. resolved
