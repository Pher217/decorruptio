"""The WSGI and ASGI entrypoints must default to the real PostgreSQL settings
module, not the SQLite dev override — manage.py was fixed for this footgun but
the server entrypoints still defaulted to config.settings.dev, so a deploy
would have silently served an empty local SQLite file with no error."""

import importlib
import os
from unittest.mock import patch


def test_wsgi_defaults_to_base_settings_when_unset(monkeypatch):
    """GIVEN DJANGO_SETTINGS_MODULE is unset
    WHEN config.wsgi is imported
    THEN it defaults to config.settings.base (PostgreSQL), not config.settings.dev (SQLite)."""
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    with patch("django.core.wsgi.get_wsgi_application"):
        importlib.reload(importlib.import_module("config.wsgi"))
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.base"


def test_wsgi_respects_explicit_dev_opt_in(monkeypatch):
    """GIVEN DJANGO_SETTINGS_MODULE is explicitly set to config.settings.dev
    WHEN config.wsgi is imported
    THEN the explicit opt-in is preserved and not overridden by the safe default."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    with patch("django.core.wsgi.get_wsgi_application"):
        importlib.reload(importlib.import_module("config.wsgi"))
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.dev"


def test_asgi_defaults_to_base_settings_when_unset(monkeypatch):
    """GIVEN DJANGO_SETTINGS_MODULE is unset
    WHEN config.asgi is imported
    THEN it defaults to config.settings.base (PostgreSQL), not config.settings.dev (SQLite)."""
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    with patch("django.core.asgi.get_asgi_application"):
        importlib.reload(importlib.import_module("config.asgi"))
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.base"


def test_asgi_respects_explicit_dev_opt_in(monkeypatch):
    """GIVEN DJANGO_SETTINGS_MODULE is explicitly set to config.settings.dev
    WHEN config.asgi is imported
    THEN the explicit opt-in is preserved and not overridden by the safe default."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    with patch("django.core.asgi.get_asgi_application"):
        importlib.reload(importlib.import_module("config.asgi"))
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.dev"
