"""Indicator discovery via the 'uncorrupt.indicators' entry-point group.

Enforces disabled-unless-validated: `enabled_for(locale)` only returns indicators
explicitly VALIDATED for that locale.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from uncorrupt.indicators.base import Indicator

_GROUP = "uncorrupt.indicators"


def load_indicators() -> dict[str, Indicator]:
    return {ep.name: ep.load()() for ep in entry_points(group=_GROUP)}


def enabled_for(locale_code: str) -> list[Indicator]:
    return [i for i in load_indicators().values() if i.runs_in(locale_code)]
