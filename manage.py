#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""

import os
import sys


def main() -> None:
    # Default to the real PostgreSQL target (config.settings.base), not the
    # SQLite dev override. A bare `manage.py <cmd>` used to silently default
    # to config.settings.dev (SQLite) and query an empty local file with no
    # error — the same footgun scripts/*.py were fixed for previously. Opt
    # into SQLite explicitly: `DJANGO_SETTINGS_MODULE=config.settings.dev`.
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
