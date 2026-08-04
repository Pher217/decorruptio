"""Provenance + version stamps — every datum and every flag carries these (ADR-000 G6).

This is what makes a finding reproducible and defensible rather than a rumor.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from uncorrupt.core.fx import FxProvenance
from uncorrupt.core.tiers import DataClass, Tier


class Redistribution(StrEnum):
    OPEN = "open"  # freely redistributable
    ATTRIBUTION = "attribution"  # redistributable with attribution
    # e.g. OpenSanctions CC-BY-NC — excluded from bulk open export
    NON_COMMERCIAL = "non_commercial"
    NO_REDISTRIBUTION = "no_redistribution"


@dataclass(frozen=True)
class ProvenanceRecord:
    source_id: str
    source_url: str
    retrieved_at: datetime
    content_hash: str  # sha256 of the raw payload
    license: str
    redistribution: Redistribution
    jurisdiction: str
    data_class: DataClass
    tier: Tier
    connector: str
    connector_version: str
    fx: FxProvenance | None = None
    # When the SOURCE published or captured this artifact -- e.g. a Wayback
    # Machine snapshot timestamp -- NEVER when we downloaded it. A live fetch
    # with no capture date of its own (a REST endpoint reflecting current
    # state) leaves this None rather than defaulting it to `retrieved_at`:
    # a bug once populated a historical Wayback capture's evidence with
    # today's download time, silently destroying its value as pre-award
    # evidence (see `uncorrupt.staging.raw`, the shared cache helper this
    # field exists to make that mistake hard to repeat in).
    observed_at: datetime | None = None


@dataclass(frozen=True)
class VersionStamp:
    """Stamped on every flag/export so (same inputs -> same flags) is auditable."""

    data_snapshot: str
    code_version: str
    indicator_version: str | None = None
    resolver_version: str | None = None
    model_version: str | None = None
