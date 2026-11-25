import os
from pathlib import Path
import dj_database_url
import environ

# Build paths inside the project like this: BASE_DIR / 'subdir'.
BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, ["127.0.0.1", "localhost"]),
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

# --- Campus verification (apps/accounts/identity.py) ---
# Separate from SECRET_KEY on purpose, so rotating SECRET_KEY doesn't orphan every registration-
# number binding. No committed default: dev.py supplies throwaway values, prod.py requires real ones.
# If this key leaks together with the DB, the 8-digit reg-no space is brute-forced in seconds, so it
# lives only in the host's env, never in a backup or the repo.
REG_NO_HMAC_KEY = env("REG_NO_HMAC_KEY", default="")
# Comma-separated Fernet keys for the card name. The FIRST encrypts; all are tried for decrypting, so
# rotation is: prepend a new key, re-encrypt lazily, drop the old one later.
CARD_NAME_KEYS = env.list("CARD_NAME_KEYS", default=[])
# What a normalized registration number must look like (after stripping spaces and dashes).
REG_NO_PATTERN = env("REG_NO_PATTERN", default=r"^\d{8}$")
# Exact-match email domains that count as campus addresses. NOT a suffix match: "notlpu.co.in" and
# "lpu.co.in.evil.com" must not pass. Used only to auto-verify at email confirmation (never to gate
# signup, and never as a role signal: students and staff both have these addresses).
ALLOWED_CAMPUS_DOMAINS = [
    d.strip().lower().lstrip("@") for d in env.list("ALLOWED_CAMPUS_DOMAINS", default=["lpu.co.in"]) if d.strip()
]
# Exact email addresses (comma-separated) that are granted the staff role when they confirm their
# address. Staff is never inferred from a domain: students hold @lpu.co.in addresses too.
STAFF_EMAIL_ALLOWLIST = [e.strip().lower() for e in env.list("STAFF_EMAIL_ALLOWLIST", default=[]) if e.strip()]
# Per-verifier caps on in-person verifications, so a compromised verifier account can't mass-verify.
# Counted from the AuditEvent table (survives restarts, unlike a LocMem counter).
VERIFIER_LIMIT_HOURLY = env.int("VERIFIER_LIMIT_HOURLY", default=30)
VERIFIER_LIMIT_DAILY = env.int("VERIFIER_LIMIT_DAILY", default=150)
# Live OPEN bounties a user may have: email-verified users 1, campus-verified users 5 (hidden ones
# count). A lapsed or revoked verification drops back to the lower cap; bounties above it are left alone.
OPEN_BOUNTY_CAP_EMAIL_VERIFIED = env.int("OPEN_BOUNTY_CAP_EMAIL_VERIFIED", default=1)
OPEN_BOUNTY_CAP_CAMPUS_VERIFIED = env.int("OPEN_BOUNTY_CAP_CAMPUS_VERIFIED", default=5)
# How long a campus verification stays active before it must be renewed.
VERIFICATION_TTL_DAYS = env.int("VERIFICATION_TTL_DAYS", default=365)
# Renewal reminders: sent once per verification this many days before it lapses, at most this many per
# rolling 24 h (email is limited to 300/day and verification, reset and acceptance mail come first).
RENEWAL_REMINDER_DAYS = env.int("RENEWAL_REMINDER_DAYS", default=30)
RENEWAL_REMINDERS_PER_DAY = env.int("RENEWAL_REMINDERS_PER_DAY", default=50)

# Outbox email delivery. The provider's budget is 300/day and account verification and password reset
# come first, so notification mail is capped well below it.
OUTBOX_EMAIL_DAILY_CAP = env.int("OUTBOX_EMAIL_DAILY_CAP", default=150)
OUTBOX_MAX_ATTEMPTS = env.int("OUTBOX_MAX_ATTEMPTS", default=6)
OUTBOX_DRAIN_LIMIT = env.int("OUTBOX_DRAIN_LIMIT", default=20)
# Shared secret for POST /internal/tick (header X-Tick-Secret). Empty disables the endpoint (404).
TICK_SECRET = env("TICK_SECRET", default="")

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

AUTH_USER_MODEL = "accounts.User"

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "apps.accounts.middleware.VerifiedEmailMiddleware",
    # Default-deny: every view needs a login unless it is marked @login_not_required (the
    # public pages, auth flow and health checks). Must come after VerifiedEmailMiddleware so
    # an unverified session is already anonymous by the time this runs.
    "django.contrib.auth.middleware.LoginRequiredMiddleware",
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
LOGIN_REDIRECT_URL = "board"  # root URLconf name for "/board/"
LOGOUT_REDIRECT_URL = "landing"

# Messages framework - map to neo-brutalist alert styles
from django.contrib.messages import constants as messages
MESSAGE_TAGS = {
    messages.DEBUG: "debug",
    messages.INFO: "info",
    messages.SUCCESS: "success",
    messages.WARNING: "warning",
    messages.ERROR: "danger",
}

# How long an emailed verification link stays valid.
EMAIL_VERIFICATION_TTL_HOURS = 24

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
    # Resending a verification link sends email (300/day budget): limit per mailbox and per IP.
    # Applied whether or not the address has an account, so responses stay identical.
    "verify_resend_per_email": "3/h",
    "verify_resend_per_ip": "30/h",
    # Every desk submission, successful or not, per verifier. The service caps successful
    # verifications; this caps *guessing* presence codes (a wrong code is not a verification).
    "desk_per_verifier": "60/h",
    # Renewal sends a link to the campus mailbox (email budget), so limit it per account.
    "renew_request_per_user": "3/h",
    "renew_confirm_per_user": "20/h",
}

# Cache backend (LocMem for dev & rate limiting)
CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
        "LOCATION": "bountyboard-default-cache",
    }
}
