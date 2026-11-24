from .base import *  # noqa: F403

DEBUG = True

# Throwaway development-only defaults. prod.py rejects this key.
SECRET_KEY = SECRET_KEY or "insecure-default-django-key-for-dev-builds-only-1234567890"  # noqa: F405
SITE_URL = SITE_URL or "http://localhost:8000"  # noqa: F405
# Throwaway development-only keys. Never used in production (prod.py requires its own).
REG_NO_HMAC_KEY = REG_NO_HMAC_KEY or "dev-only-reg-no-hmac-key-not-for-production-0123456789"  # noqa: F405
CARD_NAME_KEYS = CARD_NAME_KEYS or ["Jcr23Z9b0ORKdGnQQibh8ZEurkoxmpn_qvy-kcKfmss="]  # noqa: F405

# Add django-debug-toolbar
try:
    import debug_toolbar  # noqa: F401
    INSTALLED_APPS += ["debug_toolbar"]  # noqa: F405
    # Insert DebugToolbarMiddleware right after GZipMiddleware or early
    MIDDLEWARE.insert(0, "debug_toolbar.middleware.DebugToolbarMiddleware")  # noqa: F405
    INTERNAL_IPS = ["127.0.0.1", "localhost"]
except ImportError:
    pass

STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
