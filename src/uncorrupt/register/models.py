"""Pydantic models for the legal-basis + redistribution register and locale profiles."""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from uncorrupt.core.provenance import Redistribution
from uncorrupt.core.tiers import DataClass, Tier


class SourceEntry(BaseModel):
    """One `sources/<source_id>.yml` entry. A connector cannot run without one."""

    source_id: str
    name: str
    jurisdictions: list[str]          # ISO codes or ["GLOBAL"]
    data_class: DataClass
    tier: Tier                        # default publication tier for this source
    license: str
    redistribution: Redistribution
    legal_basis: str
    access_method: str                # bulk-api | open-data-dump | scrape | ocr
    robots_tos_reviewed: date | None = None
    freshness_sla_days: int
    dpia_cleared: bool = False        # A2 connectors refuse to load unless True
    notes: str | None = None


class LocaleProfile(BaseModel):
    """One `locales/<code>.yml` entry."""

    code: str
    name_normalization_profile: str = "passthrough"
    currency: str | None = None
    procedure_metadata: dict = {}
    notes: str | None = None
