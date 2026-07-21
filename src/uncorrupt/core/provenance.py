"""Provenance + version stamps — every datum and every flag carries these (ADR-000 G6).

This is what makes a finding reproducible and defensible rather than a rumor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from uncorrupt.core.fx import FxProvenance
from uncorrupt.core.tiers import DataClass, Tier


class Redistribution(str, Enum):
    OPEN = "open"                       # freely redistributable
    ATTRIBUTION = "attribution"         # redistributable with attribution
    NON_COMMERCIAL = "non_commercial"   # e.g. OpenSanctions CC-BY-NC — excluded from bulk open export
    NO_REDISTRIBUTION = "no_redistribution"


@dataclass(frozen=True)
class ProvenanceRecord:
    source_id: str
    source_url: str
    retrieved_at: datetime
    content_hash: str                 # sha256 of the raw payload
    license: str
    redistribution: Redistribution
    jurisdiction: str
    data_class: DataClass
    tier: Tier
    connector: str
    connector_version: str
    fx: FxProvenance | None = None


@dataclass(frozen=True)
class VersionStamp:
    """Stamped on every flag/export so (same inputs -> same flags) is auditable."""

    data_snapshot: str
    code_version: str
    indicator_version: str | None = None
    resolver_version: str | None = None
    model_version: str | None = None
