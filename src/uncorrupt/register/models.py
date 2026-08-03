"""Pydantic models for the legal-basis + redistribution register and locale profiles."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, model_validator

from uncorrupt.core.provenance import Redistribution
from uncorrupt.core.tiers import DataClass, Tier


class SourceEntry(BaseModel):
    """One `sources/<source_id>.yml` entry. A connector cannot run without one.

    Shared by both connector families: `connector_kind: procurement` (the OCDS/OCP
    pipeline, driven by `Connector`/`EvaluationContext`) and `connector_kind: graph`
    (the relationship-recovery layer under `uncorrupt.graph`, one module per source).
    A single register + a `connector_kind` discriminator — not a parallel schema —
    because "does this source have a legal basis to run" is the same question for
    both families; only the extra fields graph connectors must additionally declare
    differ (see `_graph_connector_declares_its_contract` and `sources/README.md`).
    """

    source_id: str
    name: str
    jurisdictions: list[str]  # ISO codes or ["GLOBAL"]
    data_class: DataClass
    tier: Tier  # default publication tier for this source
    license: str
    redistribution: Redistribution
    legal_basis: str
    access_method: str  # bulk-api | open-data-dump | scrape | ocr
    robots_tos_reviewed: date | None = None
    freshness_sla_days: int
    dpia_cleared: bool = False  # A2 connectors refuse to load unless True
    notes: str | None = None

    # --- graph/relationship-connector contract (the analogue of the procurement
    # Connector protocol for uncorrupt.graph modules) ---
    connector_kind: Literal["procurement", "graph"] = "procurement"
    locale: str | None = None  # locales/<code>.yml this connector's data is denominated
    # in; omit only when jurisdictions == ["GLOBAL"] (e.g. GLEIF has no single locale).
    registry_schemes: list[str] = []  # Entity.registry_scheme values this connector emits
    identifier_field: str | None = None  # what a graph connector resolves/dedupes entities on
    rate_limit: str | None = None  # free-text rate-limit / politeness description

    @model_validator(mode="after")
    def _graph_connector_declares_its_contract(self) -> SourceEntry:
        """A `connector_kind: graph` entry has no optional fields (ADR-001 D5,
        extended to the graph layer): a country-replicable connector must state
        its locale, the registry schemes it emits, its identifier, and its rate
        limit, or the register entry itself is incomplete."""
        if self.connector_kind != "graph":
            return self
        missing = []
        if "GLOBAL" not in self.jurisdictions and not self.locale:
            missing.append("locale")
        if not self.registry_schemes:
            missing.append("registry_schemes")
        if not self.identifier_field:
            missing.append("identifier_field")
        if not self.rate_limit:
            missing.append("rate_limit")
        if missing:
            raise ValueError(
                f"source_id={self.source_id!r}: connector_kind 'graph' requires {missing}"
            )
        return self


class LocaleProfile(BaseModel):
    """One `locales/<code>.yml` entry."""

    code: str
    name_normalization_profile: str = "passthrough"
    currency: str | None = None
    procedure_metadata: dict = {}
    notes: str | None = None
