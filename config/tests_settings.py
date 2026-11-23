"""Production settings must fail closed, and wsgi/asgi must default to them."""
import ast
import importlib
import os
import sys
from pathlib import Path
from unittest import mock

from django.core.exceptions import ImproperlyConfigured
from django.conf import settings
from django.test import SimpleTestCase
from django.urls import reverse

CONFIG_DIR = Path(__file__).resolve().parent
GOOD_KEY = "k" * 20 + "Zx9-" * 10  # 60 chars, not on the insecure list
VALID_ENV = {
    "SECRET_KEY": GOOD_KEY,
    "ALLOWED_HOSTS": "bountyboard.example.com",
    "CSRF_TRUSTED_ORIGINS": "https://bountyboard.example.com",
    "SITE_URL": "https://bountyboard.example.com",
}
SETTINGS_MODULES = ("config.settings.base", "config.settings.dev", "config.settings.prod")


class ProdSettingsFailClosedTests(SimpleTestCase):
    def load(self, module="config.settings.prod", **overrides):
        """Import a settings module fresh, under exactly this environment (no .env, no inherited vars)."""
        env = {**VALID_ENV, **overrides} if module.endswith("prod") else dict(overrides)
        env = {k: v for k, v in env.items() if v is not None}
        saved = {m: sys.modules.pop(m, None) for m in SETTINGS_MODULES}
        try:
            with mock.patch.dict(os.environ, env, clear=True), mock.patch("environ.Env.read_env"):
                return importlib.import_module(module)
        finally:
            for m in SETTINGS_MODULES:
                sys.modules.pop(m, None)
                if saved[m] is not None:
                    sys.modules[m] = saved[m]

    def test_valid_environment_boots(self):
        s = self.load()
        self.assertFalse(s.DEBUG)
        self.assertEqual(s.ALLOWED_HOSTS, ["bountyboard.example.com"])
        self.assertEqual(s.CSRF_TRUSTED_ORIGINS, ["https://bountyboard.example.com"])
        self.assertEqual(s.SITE_URL, "https://bountyboard.example.com")
        self.assertEqual(s.SECRET_KEY, GOOD_KEY)

    def test_each_required_variable_is_required(self):
        for name in VALID_ENV:
            with self.subTest(missing=name):
                with self.assertRaisesMessage(ImproperlyConfigured, name):
                    self.load(**{name: None})
            with self.subTest(blank=name):
                with self.assertRaisesMessage(ImproperlyConfigured, name):
                    self.load(**{name: "  "})

    def test_insecure_secret_keys_are_rejected(self):
        bad = {
            "committed dev default": "insecure-default-django-key-for-dev-builds-only-1234567890",
            "django-insecure prefix": "django-insecure-" + "x" * 60,
            "too short": "short-but-unique-Zx9",
        }
        for label, key in bad.items():
            with self.subTest(label):
                with self.assertRaisesMessage(ImproperlyConfigured, "SECRET_KEY"):
                    self.load(SECRET_KEY=key)

    def test_wildcard_and_plain_http_are_rejected(self):
        cases = [
            ("ALLOWED_HOSTS", "*", "ALLOWED_HOSTS"),
            ("ALLOWED_HOSTS", "good.example.com,*", "ALLOWED_HOSTS"),
            ("CSRF_TRUSTED_ORIGINS", "https://*.onrender.com", "CSRF_TRUSTED_ORIGINS"),
            ("CSRF_TRUSTED_ORIGINS", "http://bountyboard.example.com", "CSRF_TRUSTED_ORIGINS"),
            ("SITE_URL", "http://bountyboard.example.com", "SITE_URL"),
        ]
        for name, value, expected in cases:
            with self.subTest(name=name, value=value):
                with self.assertRaisesMessage(ImproperlyConfigured, expected):
                    self.load(**{name: value})

    def test_dev_settings_still_boot_without_any_environment(self):
        s = self.load("config.settings.dev")
        self.assertTrue(s.DEBUG)
        self.assertTrue(s.SECRET_KEY)
        self.assertEqual(s.SITE_URL, "http://localhost:8000")

    def test_base_settings_have_no_committed_secret_key(self):
        s = self.load("config.settings.base")
        self.assertEqual(s.SECRET_KEY, "")


class EntryPointDefaultsTests(SimpleTestCase):
    def default_settings_module(self, filename):
        tree = ast.parse((CONFIG_DIR / filename).read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "setdefault"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "DJANGO_SETTINGS_MODULE"
            ):
                return node.args[1].value
        self.fail(f"{filename} does not set a default DJANGO_SETTINGS_MODULE")

    def test_wsgi_and_asgi_default_to_production_settings(self):
        for filename in ("wsgi.py", "asgi.py"):
            with self.subTest(filename):
                self.assertEqual(self.default_settings_module(filename), "config.settings.prod")


class AuthRedirectSettingsTests(SimpleTestCase):
    def test_login_redirect_is_the_board_and_logout_redirect_is_the_landing_page(self):
        # "pages:board" did not exist, so reverse() raised NoReverseMatch at login/logout time.
        self.assertEqual(reverse(settings.LOGIN_REDIRECT_URL), "/board/")
        self.assertEqual(reverse(settings.LOGOUT_REDIRECT_URL), "/")
