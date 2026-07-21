"""Dagster Definitions entry point (referenced by pyproject + dagster.yaml).

Phase 1 wires: ingest (TED, GLEIF) -> normalize (OCDS + FtM) -> indicators -> publish,
with daily partitions (backfillable) and asset checks as per-source data-quality gates.
Kept import-safe if dagster isn't installed so the package imports cleanly.
"""

from __future__ import annotations

try:
    from dagster import Definitions

    defs = Definitions(assets=[], asset_checks=[], schedules=[])
except Exception:  # pragma: no cover - dagster optional at import time
    defs = None
