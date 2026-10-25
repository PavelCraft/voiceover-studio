import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "local-development-only-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = [host.strip() for host in os.environ.get("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if host.strip()]
CSRF_TRUSTED_ORIGINS = [origin.strip() for origin in os.environ.get("DJANGO_CSRF_TRUSTED_ORIGINS", "").split(",") if origin.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "studio",
    "users",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "studio.middleware.StorageQuotaMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "voiceover_studio.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [BASE_DIR / "templates"],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
    ]},
}]
WSGI_APPLICATION = "voiceover_studio.wsgi.application"
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": Path(os.environ.get("DJANGO_DB_PATH", BASE_DIR / "db.sqlite3"))}}
AUTH_PASSWORD_VALIDATORS = []
LANGUAGE_CODE = "en"
LANGUAGES = [
    ("ru", "Russian"),
    ("en", "English"),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
TIME_ZONE = "Europe/Moscow"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = Path(os.environ.get("DJANGO_STATIC_ROOT", BASE_DIR / "staticfiles"))
STATICFILES_DIRS = [BASE_DIR / "studio" / "static"]
MEDIA_URL = "/media/"
MEDIA_ROOT = Path(os.environ.get("DJANGO_MEDIA_ROOT", BASE_DIR / "media"))
FILE_UPLOAD_TEMP_DIR = os.environ.get("DJANGO_UPLOAD_TEMP_DIR") or None
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
VOICEOVER_AUTOSTART_WORKER = os.environ.get("VOICEOVER_AUTOSTART_WORKER", "1") == "1"
VOICEOVER_SHOW_LOCAL_PATHS = os.environ.get(
    "VOICEOVER_SHOW_LOCAL_PATHS", "1" if DEBUG else "0"
) == "1"
GROQ_TEXT_MODEL = os.environ.get("GROQ_TEXT_MODEL", "llama-3.3-70b-versatile")
VOICEOVER_MEDIA_LIMIT_BYTES = int(os.environ.get("VOICEOVER_MEDIA_LIMIT_BYTES", 6 * 1024**3))
VOICEOVER_DISK_RESERVE_BYTES = int(os.environ.get("VOICEOVER_DISK_RESERVE_BYTES", 8 * 1024**3))
VOICEOVER_DISK_CHECK_PATH = Path(os.environ.get("VOICEOVER_DISK_CHECK_PATH", MEDIA_ROOT))

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
USE_X_FORWARDED_HOST = True
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG

LOGIN_URL = "users:login"
LOGIN_REDIRECT_URL = "dashboard"
