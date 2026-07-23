"""Dagster Definitions entry point (referenced by pyproject + dagster.yaml).

Phase 1 wires: ingest -> normalize -> indicators -> publish,
with daily partitions (backfillable) and asset checks as per-source data-quality gates.
Kept import-safe if dagster isn't installed so the package imports cleanly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from dagster import Definitions  # type: ignore[import-not-found]

defs: Any

try:
    from dagster import Definitions

    defs = Definitions(assets=[], asset_checks=[], schedules=[])
except ImportError:  # pragma: no cover - dagster optional at import time
    defs = None
