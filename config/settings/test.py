"""Test settings — uses SQLite for fast test runs (no PostgreSQL needed in CI)."""

from config.settings.base import *  # noqa: F401,F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}
