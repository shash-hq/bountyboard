import os
from pathlib import Path
import dj_database_url
import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["127.0.0.1", "localhost"]),
    ALLOWED_EMAIL_DOMAIN=(str, ""),
    DATABASE_URL=(str, f"sqlite:///{BASE_DIR / 'db.sqlite3'}"),
    SECURE_SSL_REDIRECT=(bool, False),
    SESSION_COOKIE_SECURE=(bool, False),
    CSRF_COOKIE_SECURE=(bool, False),
    CSRF_TRUSTED_ORIGINS=(list, []),
)

# Read .env file if it exists
env_file = BASE_DIR / ".env"
if env_file.exists():
    environ.Env.read_env(str(env_file))

# No committed default: dev.py supplies a throwaway key, prod.py refuses to boot without a real one.
SECRET_KEY = env("SECRET_KEY", default="")
# Public base URL, used for absolute links (emails). dev.py defaults it, prod.py requires it.
SITE_URL = env("SITE_URL", default="")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")
if "testserver" not in ALLOWED_HOSTS:
    ALLOWED_HOSTS.append("testserver")
ALLOWED_EMAIL_DOMAIN = env("ALLOWED_EMAIL_DOMAIN")

# Application definition
DJANGO_APPS = [
    "whitenoise.runserver_nostatic",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "django.contrib.sitemaps",
]

THIRD_PARTY_APPS = [
    # django_ratelimit does not need to be in INSTALLED_APPS, but we keep hooks ready
]

LOCAL_APPS = [
    "apps.accounts.apps.AccountsConfig",
    "apps.bounties.apps.BountiesConfig",
    "apps.pages.apps.PagesConfig",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django_ratelimit.middleware.RatelimitMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.pages.context_processors.global_ui_context",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

# Database
DATABASES = {
    "default": dj_database_url.config(
        default=env("DATABASE_URL"),
        conn_max_age=600,
        conn_health_checks=True,
    )
}

# Password validation
AUTH_PASSWORD_VALIDATORS = [
    {
        "NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.MinimumLengthValidator",
        "OPTIONS": {"min_length": 8},
    },
    {
        "NAME": "django.contrib.auth.password_validation.CommonPasswordValidator",
    },
    {
        "NAME": "django.contrib.auth.password_validation.NumericPasswordValidator",
    },
]

# Internationalization
LANGUAGE_CODE = "en-in"
TIME_ZONE = "Asia/Kolkata"
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Default primary key field type
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Auth Redirects
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "board"  # root URLconf name for "/"; "pages:board" does not exist
LOGOUT_REDIRECT_URL = "board"

# Messages framework - map to neo-brutalist alert styles
from django.contrib.messages import constants as messages
MESSAGE_TAGS = {
    messages.DEBUG: "debug",
    messages.INFO: "info",
    messages.SUCCESS: "success",
    messages.WARNING: "warning",
    messages.ERROR: "danger",
}

# Email backend default
EMAIL_BACKEND = "django.core.mail.backends.console.EmailBackend"
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="BountyBoard <notifications@bountyboard.campus>")

# --- Rate limiting -------------------------------------------------------------------
# Number of reverse proxies in front of the app that append to X-Forwarded-For (Render's
# load balancer = at least 1). 0 means "trust only REMOTE_ADDR": the header is then ignored,
# so a client cannot dodge limits by spoofing it. Measure on the real instance before raising it.
TRUSTED_PROXY_COUNT = env.int("TRUSTED_PROXY_COUNT", default=0)
RATELIMIT_IP_META_KEY = "config.http.client_ip"
RATELIMIT_VIEW = "apps.pages.views.ratelimited"  # renders HTTP 429
# Campus NAT means many students can share one public IP, so per-IP ceilings are generous
# floods-only limits; the tight limits are per account / per email.
RATE_LIMITS = {
    "login_failures_per_account": "10/15m",
    "login_per_ip": "1000/h",
    "signup_per_ip": "200/h",
    "password_reset_per_email": "3/h",
    "password_reset_per_ip": "30/h",
}

# Cache backend (LocMem for dev & rate limiting)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "bountyboard-default-cache",
    }
}
