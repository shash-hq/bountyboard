from .base import *  # noqa: F403

DEBUG = True

# Throwaway development-only defaults. prod.py rejects this key.
SECRET_KEY = SECRET_KEY or "insecure-default-django-key-for-dev-builds-only-1234567890"  # noqa: F405
SITE_URL = SITE_URL or "http://localhost:8000"  # noqa: F405

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
