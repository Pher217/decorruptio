"""Dev settings — SQLite for local development without PostgreSQL."""

from config.settings.base import *  # noqa: F401,F403

DEBUG = True

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "decorruptio_dev.db",
    }
}
