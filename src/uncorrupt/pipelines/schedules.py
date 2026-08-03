"""Refresh cadence — a declarative view of what's due, derived from
``freshness_sla_days`` in ``sources/*.yml`` via ``pipelines.freshness``
(ADR-001: batch, daily to start; ADR-005 rejected a scheduler daemon /
Celery / cron wiring — this project is one Postgres and a set of scripts).

No daemon lives here. ``refresh_schedule`` is a plain query a human or a
script calls ("what's due, run it") — not a thing that runs itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from uncorrupt.pipelines.freshness import FreshnessStatus, check_freshness

# A source that has run before (successfully or not) and is now stale or
# critical has breached its SLA at least once. "unknown" (never run at all)
# is due for a first run, but has no prior success to have lapsed, so it is
# not counted as overdue.
_OVERDUE_LABELS = frozenset({"stale", "critical"})


@dataclass(frozen=True)
class RefreshCadence:
    """What's due for refresh, and what's overdue, right now."""

    due: list[FreshnessStatus]
    overdue: list[FreshnessStatus]


def refresh_schedule(
    sources_dir: Path | None = None,
    now: datetime | None = None,
) -> RefreshCadence:
    """What is due for refresh, and what is overdue, for every registered source.

    ``due`` = every source not labelled ``fresh`` (``stale``, ``critical``,
    or ``unknown``/never-run). ``overdue`` = the subset that has actually
    breached its SLA window (``stale`` or ``critical``) — always a subset of
    ``due``.
    """
    statuses = check_freshness(sources_dir=sources_dir, now=now)
    return RefreshCadence(
        due=[s for s in statuses if s.label != "fresh"],
        overdue=[s for s in statuses if s.label in _OVERDUE_LABELS],
    )
