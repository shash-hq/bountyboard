"""render.yaml must stay in step with the settings it deploys (it cannot be run in CI)."""
import re
from pathlib import Path

from django.test import SimpleTestCase, TestCase

ROOT = Path(__file__).resolve().parent.parent
BLUEPRINT = (ROOT / "render.yaml").read_text()
CODE = "\n".join(re.sub(r"\s#.*$", "", line) for line in BLUEPRINT.splitlines() if not line.lstrip().startswith("#"))
PROD = (ROOT / "config/settings/prod.py").read_text()
SECRET_KEYS = ("DATABASE_URL", "EMAIL_URL", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "SITE_URL")


def env_blocks():
    """{KEY: block text} for each `- key: KEY` under envVars."""
    parts = re.split(r"\n\s*- key: ", BLUEPRINT)[1:]
    return {p.split()[0]: p for p in parts}


class RenderBlueprintTests(SimpleTestCase):
    def test_every_variable_prod_requires_is_declared(self):
        required = set(re.findall(r'_required(?:_list)?\("([A-Z_]+)"\)', PROD))
        self.assertTrue({"SECRET_KEY", "ALLOWED_HOSTS", "CSRF_TRUSTED_ORIGINS", "SITE_URL"} <= required)
        missing = (required | {"DATABASE_URL"}) - set(env_blocks())
        self.assertEqual(missing, set(), "prod.py refuses to boot without these; render.yaml must ask for them")

    def test_secrets_are_prompted_for_never_committed(self):
        blocks = env_blocks()
        for key in SECRET_KEYS:
            with self.subTest(key=key):
                self.assertIn("sync: false", blocks[key])
                self.assertNotIn("value:", blocks[key])
        self.assertIn("generateValue: true", blocks["SECRET_KEY"])
        self.assertNotIn("value:", blocks["SECRET_KEY"])

    def test_production_settings_module_is_selected(self):
        self.assertRegex(env_blocks()["DJANGO_SETTINGS_MODULE"], r"value:\s*config\.settings\.prod")

    def test_deployment_target_matches_the_plan(self):
        self.assertRegex(BLUEPRINT, r"(?m)^\s+plan: free$")
        self.assertRegex(BLUEPRINT, r"(?m)^\s+region: singapore$")
        self.assertRegex(BLUEPRINT, r"(?m)^\s+startCommand: .*--workers 1 --threads 4")
        self.assertRegex(BLUEPRINT, r"(?m)^\s+healthCheckPath: /healthz")
        self.assertNotIn("databases:", CODE)  # the database is Supabase, not Render

    def test_trusted_proxy_count_is_not_guessed(self):
        self.assertNotIn("TRUSTED_PROXY_COUNT", env_blocks())

    def test_migrations_run_at_build_time_not_on_boot(self):
        self.assertRegex(BLUEPRINT, r"(?m)^\s+buildCommand: .*manage\.py migrate --noinput")
        self.assertNotRegex(BLUEPRINT, r"(?m)^\s+startCommand: .*migrate")


class HealthzTests(TestCase):
    def test_healthz_does_not_touch_the_database(self):
        with self.assertNumQueries(0):
            r = self.client.get("/healthz")
        self.assertEqual(r.status_code, 200)
