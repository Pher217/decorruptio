"""Dev settings — SQLite for local development without PostgreSQL.

Opt-in only. Ingest/staging scripts must NOT default to this module — the
staging PostgreSQL database is the only DB with real data. Use
config.settings.base (PostgreSQL, from DATABASE_URL) unless you deliberately
want an empty local SQLite file.
"""

from config.settings.base import *  # noqa: F401,F403
from config.settings.base import log_db_target

DEBUG = True

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": "decorruptio_dev.db",
    }
}

log_db_target(DATABASES, label="DB target (dev override: SQLite)")
