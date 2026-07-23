"""DuckDB staging layer — lightweight A1 stack per ADR-002.

Sits between connectors (raw fetch) and indicators (anomaly detection).
Provides a unified OCDS-flattened schema that all three sources map into,
regardless of whether they're native OCDS (UK), near-OCDS (Ukraine), or
non-OCDS (Colombia SECOP II).
"""

from uncorrupt.staging.ingest import ingest_artifacts
from uncorrupt.staging.queries import get_awards, get_bids, get_tenders
from uncorrupt.staging.schema import create_schema, drop_schema

__all__ = [
    "create_schema",
    "drop_schema",
    "ingest_artifacts",
    "get_tenders",
    "get_awards",
    "get_bids",
]
