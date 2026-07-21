"""Bulk open-data exports (tier-a). MUST pass register.enforcement gates (ADR-000 G2 / D5-bis)."""

from __future__ import annotations

from collections.abc import Iterable

from uncorrupt.core.provenance import ProvenanceRecord
from uncorrupt.register.enforcement import filter_bulk_open_exportable


def open_export(records: Iterable[ProvenanceRecord]) -> list[ProvenanceRecord]:
    """Return only records lawfully includable in a bulk open export."""
    return filter_bulk_open_exportable(records)
