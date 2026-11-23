import os

import dj_database_url
from django.core.exceptions import ImproperlyConfigured

from .base import *  # noqa: F403

DEBUG = False

# --- Fail closed: production refuses to boot with missing or unsafe security settings. ---
# Values are read straight from os.environ (base.py already loaded .env into it), because
# django-environ would silently fall back to the development defaults declared in base.py.


def _required(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise ImproperlyConfigured(f"{name} must be set in production (refusing to boot with a default).")
    return value


def _required_list(name):
    values = [v.strip() for v in _required(name).split(",") if v.strip()]
    if not values:
        raise ImproperlyConfigured(f"{name} must list at least one value in production.")
    return values


_KNOWN_INSECURE_KEYS = {"insecure-default-django-key-for-dev-builds-only-1234567890"}

SECRET_KEY = _required("SECRET_KEY")
if SECRET_KEY in _KNOWN_INSECURE_KEYS or SECRET_KEY.startswith(("insecure", "django-insecure-")) or len(SECRET_KEY) < 50:
    raise ImproperlyConfigured("SECRET_KEY is a known/insecure value or shorter than 50 characters.")

ALLOWED_HOSTS = _required_list("ALLOWED_HOSTS")
if "*" in ALLOWED_HOSTS:
    raise ImproperlyConfigured("ALLOWED_HOSTS must not contain '*' in production.")

CSRF_TRUSTED_ORIGINS = _required_list("CSRF_TRUSTED_ORIGINS")
for _origin in CSRF_TRUSTED_ORIGINS:
    if "*" in _origin or not _origin.startswith("https://"):
        raise ImproperlyConfigured(f"CSRF_TRUSTED_ORIGINS entries must be explicit https:// origins, got {_origin!r}.")

SITE_URL = _required("SITE_URL").rstrip("/")
if not SITE_URL.startswith("https://"):
    raise ImproperlyConfigured("SITE_URL must be an https:// URL in production.")

# Campus-verification keys. Separate from SECRET_KEY so rotating one doesn't break the other.
REG_NO_HMAC_KEY = _required("REG_NO_HMAC_KEY")
if len(REG_NO_HMAC_KEY) < 32 or REG_NO_HMAC_KEY == SECRET_KEY or REG_NO_HMAC_KEY.startswith("dev-only"):
    raise ImproperlyConfigured("REG_NO_HMAC_KEY must be at least 32 characters and distinct from SECRET_KEY.")

CARD_NAME_KEYS = _required_list("CARD_NAME_KEYS")
if len(set(CARD_NAME_KEYS)) != len(CARD_NAME_KEYS):
    raise ImproperlyConfigured("CARD_NAME_KEYS must not repeat a key.")
for _key in CARD_NAME_KEYS:
    try:
        from cryptography.fernet import Fernet

        Fernet(_key)
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured(
            "CARD_NAME_KEYS entries must be valid Fernet keys (32 url-safe base64 bytes); "
            "generate one with: python -c 'from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'"
        ) from exc
if "Jcr23Z9b0ORKdGnQQibh8ZEurkoxmpn_qvy-kcKfmss=" in CARD_NAME_KEYS:
    raise ImproperlyConfigured("CARD_NAME_KEYS contains the committed development key.")

# Security hardening
SECURE_SSL_REDIRECT = env.bool("SECURE_SSL_REDIRECT", default=True)
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=True)
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=True)
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"
SECURE_HSTS_SECONDS = 31536000  # 1 year
SECURE_HSTS_INCLUDE_SUBDOMAINS = True
SECURE_HSTS_PRELOAD = True
SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

# Production WhiteNoise storage
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage",
    },
}

# Production Email
if env("EMAIL_URL", default=None):
    EMAIL_CONFIG = env.email_url("EMAIL_URL")
    vars().update(EMAIL_CONFIG)

# Production Logging
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "verbose": {
            "format": "[{asctime}] {levelname} {name}:{lineno} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "verbose",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": "INFO",
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": "INFO",
            "propagate": False,
        },
        "django.security": {
            "handlers": ["console"],
            "level": "WARNING",
            "propagate": False,
        },
    },
}
