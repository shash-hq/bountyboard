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
    "DATABASE_URL": "postgres://app:pw@db.example.com:5432/postgres",
    "REG_NO_HMAC_KEY": "r" * 16 + "Qw3-" * 8,  # 48 chars, distinct from SECRET_KEY
    "CARD_NAME_KEYS": "Zm9vYmFyLWNhcmQtbmFtZS1rZXktMzItYnl0ZXMhISE=",  # 32 bytes, url-safe b64
}
SETTINGS_MODULES = ("config.settings.base", "config.settings.dev", "config.settings.prod", "config.settings.test")


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

    def test_production_never_uses_the_fast_test_hasher(self):
        """config.settings.test swaps in MD5 for speed; that must stay confined to the test settings."""
        for module in ("config.settings.base", "config.settings.dev", "config.settings.prod"):
            self.assertFalse(hasattr(self.load(module), "PASSWORD_HASHERS"), module)  # Django's default (PBKDF2) applies
        self.assertEqual(
            self.load("config.settings.test").PASSWORD_HASHERS, ["django.contrib.auth.hashers.MD5PasswordHasher"]
        )

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

    def test_database_url_must_be_a_postgres_url_never_sqlite(self):
        bad = {
            "sqlite file": "sqlite:////tmp/db.sqlite3",
            "sqlite memory": "sqlite://:memory:",
            "mysql": "mysql://u:p@h/db",
            "no scheme": "db.example.com/postgres",
        }
        for label, value in bad.items():
            with self.subTest(label):
                with self.assertRaisesMessage(ImproperlyConfigured, "DATABASE_URL"):
                    self.load(DATABASE_URL=value)

    def test_database_settings_come_from_database_url(self):
        s = self.load(DATABASE_URL="postgresql://app:pw@aws-0.pooler.example.com:5432/postgres")
        db = s.DATABASES["default"]
        self.assertTrue(db["ENGINE"].endswith("postgresql"))
        self.assertEqual((db["HOST"], db["PORT"], db["NAME"]), ("aws-0.pooler.example.com", 5432, "postgres"))
        self.assertEqual(db["CONN_MAX_AGE"], 600)

    def test_hmac_key_must_be_long_and_distinct_from_secret_key(self):
        for label, key in {"short": "short", "same as SECRET_KEY": GOOD_KEY}.items():
            with self.subTest(label):
                with self.assertRaisesMessage(ImproperlyConfigured, "REG_NO_HMAC_KEY"):
                    self.load(REG_NO_HMAC_KEY=key)

    def test_card_name_keys_must_be_valid_unique_and_not_the_dev_key(self):
        good = VALID_ENV["CARD_NAME_KEYS"]
        dev = "Jcr23Z9b0ORKdGnQQibh8ZEurkoxmpn_qvy-kcKfmss="
        cases = {"not a fernet key": "nope", "repeated": f"{good},{good}", "dev key": f"{good},{dev}"}
        for label, value in cases.items():
            with self.subTest(label):
                with self.assertRaisesMessage(ImproperlyConfigured, "CARD_NAME_KEYS"):
                    self.load(CARD_NAME_KEYS=value)

    def test_card_name_keys_accept_a_rotation_list(self):
        from cryptography.fernet import Fernet

        new, old = Fernet.generate_key().decode(), VALID_ENV["CARD_NAME_KEYS"]
        self.assertEqual(self.load(CARD_NAME_KEYS=f"{new},{old}").CARD_NAME_KEYS, [new, old])

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
