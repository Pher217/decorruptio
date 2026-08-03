"""manage.py must default to the real PostgreSQL settings module, not the
SQLite dev override — a bare `manage.py <cmd>` with no env var previously
queried an empty local SQLite file with no error."""

import os
from unittest.mock import patch

import manage


def test_manage_defaults_to_base_settings_when_unset(monkeypatch):
    """GIVEN DJANGO_SETTINGS_MODULE is unset
    WHEN manage.main() runs
    THEN it defaults to config.settings.base (PostgreSQL), not config.settings.dev (SQLite)."""
    monkeypatch.delenv("DJANGO_SETTINGS_MODULE", raising=False)
    with patch("django.core.management.execute_from_command_line") as mock_execute:
        manage.main()
    mock_execute.assert_called_once()
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.base"


def test_manage_respects_explicit_dev_opt_in(monkeypatch):
    """GIVEN DJANGO_SETTINGS_MODULE is explicitly set to config.settings.dev
    WHEN manage.main() runs
    THEN the explicit opt-in is preserved and not overridden by the safe default."""
    monkeypatch.setenv("DJANGO_SETTINGS_MODULE", "config.settings.dev")
    with patch("django.core.management.execute_from_command_line") as mock_execute:
        manage.main()
    mock_execute.assert_called_once()
    assert os.environ["DJANGO_SETTINGS_MODULE"] == "config.settings.dev"
