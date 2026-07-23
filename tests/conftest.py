"""Pytest configuration for Django + pytest-django."""

import os

import django


def pytest_configure() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.test")
    django.setup()
