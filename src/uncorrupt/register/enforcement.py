"""Enforcement functions called by publish/ and by the CI guardrail tests."""

from __future__ import annotations

from collections.abc import Iterable

from uncorrupt.core.errors import RedistributionViolation
from uncorrupt.core.provenance import ProvenanceRecord, Redistribution
from uncorrupt.core.tiers import assert_exportable_to_open

# Redistribution values that may appear in a bulk *open* export.
_OPEN_REDIST = {Redistribution.OPEN, Redistribution.ATTRIBUTION}


def assert_bulk_open_exportable(prov: ProvenanceRecord) -> None:
    """Raise unless a record may be included in a bulk open-data export.

    Combines the tier gate (ADR-000 G2) and the redistribution gate (ADR-001 D5-bis).
    OpenSanctions (CC-BY-NC / non_commercial) is the canonical case this excludes.
    """
    assert_exportable_to_open(prov.tier, what=f"record from {prov.source_id}")
    if prov.redistribution not in _OPEN_REDIST:
        raise RedistributionViolation(
            f"source {prov.source_id} is '{prov.redistribution.value}'"
            " and cannot be included in a bulk open export"
        )


def filter_bulk_open_exportable(
    records: Iterable[ProvenanceRecord],
) -> list[ProvenanceRecord]:
    out = []
    for r in records:
        try:
            assert_bulk_open_exportable(r)
        except Exception:  # noqa: BLE001 — excluded by policy, not an error
            continue
        out.append(r)
    return out
