"""WSGI config for decorruptio."""

import os

from django.core.wsgi import get_wsgi_application

# Default to the real PostgreSQL target, not the SQLite dev override — the
# same footgun manage.py and scripts/*.py were fixed for. Opt into SQLite
# explicitly: `DJANGO_SETTINGS_MODULE=config.settings.dev`.
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
application = get_wsgi_application()
