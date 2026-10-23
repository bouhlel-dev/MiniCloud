import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def env_bool(name, default=False):
    return os.getenv(name, str(default)).lower() in {"1", "true", "yes", "on"}


SECRET_KEY = os.getenv("DJANGO_SECRET_KEY")
if not SECRET_KEY:
    raise ImproperlyConfigured("DJANGO_SECRET_KEY must be set in the environment or .env file.")

DEBUG = env_bool("DJANGO_DEBUG")
ALLOWED_HOSTS = [host.strip() for host in os.getenv("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if host.strip()]
CORS_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv(
        "CORS_ALLOWED_ORIGINS",
        "http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174",
    ).split(",")
    if origin.strip()
]

INSTALLED_APPS = [
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "channels",
    "corsheaders",
    "accounts",
    "servers",
    "monitoring",
    "api",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "minicloud.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "minicloud.wsgi.application"
ASGI_APPLICATION = "minicloud.asgi.application"

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}",
        conn_max_age=60,
        conn_health_checks=True,
    ),
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"
LOGIN_URL = "/admin/login/"

MINICLOUD_DOCKER_TIMEOUT = int(os.getenv("MINICLOUD_DOCKER_TIMEOUT", "60"))
MINICLOUD_PORT_RANGE_START = int(os.getenv("MINICLOUD_PORT_RANGE_START", "10000"))
MINICLOUD_PORT_RANGE_END = int(os.getenv("MINICLOUD_PORT_RANGE_END", "11000"))
MINICLOUD_PUBLIC_HOST = os.getenv("MINICLOUD_PUBLIC_HOST", "localhost")
MINICLOUD_XAMPP_INSTALLER_URL = os.getenv(
    "MINICLOUD_XAMPP_INSTALLER_URL",
    "https://sourceforge.net/projects/xampp/files/XAMPP%20Linux/8.2.12/xampp-linux-x64-8.2.12-0-installer.run/download",
)
MINICLOUD_XAMPP_READY_TIMEOUT = int(os.getenv("MINICLOUD_XAMPP_READY_TIMEOUT", "1800"))

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_THROTTLE_CLASSES": ["rest_framework.throttling.UserRateThrottle"],
    "DEFAULT_THROTTLE_RATES": {"user": os.getenv("API_USER_RATE_LIMIT", "120/minute")},
}

CELERY_BROKER_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
CELERY_RESULT_BACKEND = CELERY_BROKER_URL
CELERY_TIMEZONE = TIME_ZONE
CELERY_BEAT_SCHEDULE = {
    "collect-server-metrics": {
        "task": "monitoring.tasks.collect_server_metrics",
        "schedule": 10.0,
    },
    "check-server-health": {
        "task": "monitoring.tasks.check_server_health",
        "schedule": 10.0,
    },
    "prune-metric-snapshots": {
        "task": "monitoring.tasks.prune_metric_snapshots",
        "schedule": 3600.0,
    },
}

MINICLOUD_METRIC_RETENTION_DAYS = int(os.getenv("MINICLOUD_METRIC_RETENTION_DAYS", "7"))
MINICLOUD_METRIC_INTERVAL_SECONDS = int(os.getenv("MINICLOUD_METRIC_INTERVAL_SECONDS", "10"))
MINICLOUD_HIGH_CPU_PERCENT = float(os.getenv("MINICLOUD_HIGH_CPU_PERCENT", "90"))
MINICLOUD_HIGH_MEMORY_PERCENT = float(os.getenv("MINICLOUD_HIGH_MEMORY_PERCENT", "90"))
MINICLOUD_HEALTH_MAX_RESTART_ATTEMPTS = int(os.getenv("MINICLOUD_HEALTH_MAX_RESTART_ATTEMPTS", "3"))
MINICLOUD_ALERT_COOLDOWN_SECONDS = int(os.getenv("MINICLOUD_ALERT_COOLDOWN_SECONDS", "900"))

EMAIL_BACKEND = os.getenv("DJANGO_EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = os.getenv("EMAIL_HOST", "")
EMAIL_PORT = int(os.getenv("EMAIL_PORT", "587"))
EMAIL_HOST_USER = os.getenv("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = os.getenv("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = os.getenv("DEFAULT_FROM_EMAIL", "alerts@minicloud.local")

CHANNEL_LAYERS = {
    "default": {
        "BACKEND": "channels.layers.InMemoryChannelLayer",
    },
}
if channel_redis_url := os.getenv("CHANNEL_REDIS_URL"):
    CHANNEL_LAYERS["default"] = {
        "BACKEND": "channels_redis.core.RedisChannelLayer",
        "CONFIG": {"hosts": [channel_redis_url]},
    }
