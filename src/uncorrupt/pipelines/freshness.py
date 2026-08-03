"""Freshness SLA checker + staleness labels per connector (ADR-005 D2).

Compares the most recent successful IngestRun for each source against
the ``freshness_sla_days`` defined in the source registry (``sources/*.yml``).
Covers both connector families — ``connector_kind: procurement`` and
``connector_kind: graph`` — because ``_load_source_sla`` reads every
``sources/*.yml`` regardless of kind; there is nothing graph-specific to wire
in here.

Staleness labels:
- **fresh**: last successful ingest within SLA.
- **stale**: last successful ingest exceeds SLA but is less than 2× SLA.
- **critical**: an ingest has been attempted (an ``IngestRun`` row exists,
  of any status) but the last success exceeds 2× SLA, or never succeeded.
- **unknown**: no ``IngestRun`` row exists at all for this source — it has
  never even been attempted. Distinct from ``critical`` on purpose: as of
  this writing, none of the graph-layer connectors (``ec_donations``,
  ``ch_officers``, ``ch_appointments``, ``lords_interests``,
  ``parliament_interests``, ``gleif``) write to ``IngestRun`` yet, so every
  one of them was previously indistinguishable from a source that ran and
  kept failing — reported identically as ``critical`` instead of visibly as
  "never run." ``unknown`` makes that gap visible instead of silent.

The labels are designed for public display on a dashboard — they communicate
data currency without exposing internal ingest details.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from uncorrupt.staging.models import IngestRun


@dataclass(frozen=True)
class FreshnessStatus:
    """Freshness status for a single source."""

    source_id: str
    label: str  # "fresh", "stale", "critical", "unknown"
    last_success: datetime | None
    days_since_success: float | None
    sla_days: int
    is_within_sla: bool

    def __str__(self) -> str:
        if self.days_since_success is not None:
            return (
                f"{self.source_id}: {self.label} "
                f"({self.days_since_success:.1f}d ago, SLA={self.sla_days}d)"
            )
        return f"{self.source_id}: {self.label} (no successful ingest, SLA={self.sla_days}d)"


def _load_source_sla(sources_dir: Path) -> dict[str, int]:
    """Load freshness_sla_days from all source YAML files.

    Returns a mapping of source_id → sla_days.
    """
    sla_map: dict[str, int] = {}
    if not sources_dir.exists():
        return sla_map
    for yml_file in sources_dir.glob("*.yml"):
        with open(yml_file, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data and "source_id" in data and "freshness_sla_days" in data:
            sla_map[data["source_id"]] = data["freshness_sla_days"]
    return sla_map


def _get_last_success(source_id: str) -> IngestRun | None:
    """Get the most recent successful IngestRun for a source."""
    return (
        IngestRun.objects.filter(source_id=source_id, status="success")
        .order_by("-finished_at")
        .first()
    )


def _has_any_run(source_id: str) -> bool:
    """Has this source ever been the subject of an IngestRun, of any status?

    Distinguishes "never run at all" (label ``unknown``) from "has run
    before but the last success is missing or too old" (label ``critical``).
    """
    return IngestRun.objects.filter(source_id=source_id).exists()


def _compute_label(days_since: float | None, sla_days: int, *, has_any_run: bool = True) -> str:
    """Compute staleness label from days since last success and SLA.

    ``has_any_run=False`` means no ``IngestRun`` row of any status exists for
    this source — distinct from ``critical``, which means an attempt was
    made (and failed, or succeeded too long ago). Defaults to ``True`` so
    that a caller which doesn't pass run history keeps the pre-existing
    "critical" behaviour for the no-success case.
    """
    if days_since is None:
        return "critical" if has_any_run else "unknown"
    if days_since <= sla_days:
        return "fresh"
    if days_since <= sla_days * 2:
        return "stale"
    return "critical"


def check_freshness(
    sources_dir: Path | None = None,
    now: datetime | None = None,
) -> list[FreshnessStatus]:
    """Check freshness for all registered sources.

    Args:
        sources_dir: Directory containing source YAML files.
            Defaults to ``sources/`` in the project root.
        now: Reference timestamp for staleness computation.
            Defaults to current UTC time.

    Returns:
        List of FreshnessStatus, one per registered source,
        sorted by source_id.
    """
    if sources_dir is None:
        sources_dir = Path("sources")
    if now is None:
        now = datetime.now(UTC)

    sla_map = _load_source_sla(sources_dir)
    results: list[FreshnessStatus] = []

    for source_id, sla_days in sorted(sla_map.items()):
        last_run = _get_last_success(source_id)

        if last_run and last_run.finished_at:
            last_success = last_run.finished_at
            delta = now - last_success
            days_since = delta.total_seconds() / 86400
        else:
            last_success = None
            days_since = None

        label = _compute_label(days_since, sla_days, has_any_run=_has_any_run(source_id))

        results.append(
            FreshnessStatus(
                source_id=source_id,
                label=label,
                last_success=last_success,
                days_since_success=days_since,
                sla_days=sla_days,
                is_within_sla=(days_since is not None and days_since <= sla_days),
            )
        )

    return results


def check_freshness_for_source(
    source_id: str,
    sla_days: int,
    now: datetime | None = None,
) -> FreshnessStatus:
    """Check freshness for a single source.

    Convenience function when the SLA is already known
    (e.g. from the register loader, not re-reading YAML).
    """
    if now is None:
        now = datetime.now(UTC)

    last_run = _get_last_success(source_id)

    if last_run and last_run.finished_at:
        last_success = last_run.finished_at
        delta = now - last_success
        days_since = delta.total_seconds() / 86400
    else:
        last_success = None
        days_since = None

    label = _compute_label(days_since, sla_days, has_any_run=_has_any_run(source_id))

    return FreshnessStatus(
        source_id=source_id,
        label=label,
        last_success=last_success,
        days_since_success=days_since,
        sla_days=sla_days,
        is_within_sla=(days_since is not None and days_since <= sla_days),
    )
