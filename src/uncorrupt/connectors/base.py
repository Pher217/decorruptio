"""Connector Protocol (extension point 1).

Connectors are INGESTION ONLY — no parsing. They discover work and fetch raw
payloads; the framework writes the immutable raw zone, computes content_hash, and
binds the artifact to the source's register entry. Adding a country = adding a
connector + a sources/<id>.yml entry + a locale profile (ADR-001 D1).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from uncorrupt.core.tiers import DataClass


@dataclass(frozen=True)
class FetchTask:
    """A unit of work discovered by a connector (e.g. one day, one notice batch)."""

    key: str
    params: dict


@dataclass(frozen=True)
class RawArtifact:
    """A fetched payload + minimal fetch metadata. The framework adds provenance."""

    payload: bytes
    source_url: str
    media_type: str


@runtime_checkable
class Connector(Protocol):
    source_id: str  # MUST match a sources/<source_id>.yml register entry
    jurisdictions: list[str]  # ISO codes or ["GLOBAL"]
    data_class: DataClass  # A1 in Phase 1; A2 refuses to load unless register.dpia_cleared

    def discover(self, since: date | None = None) -> Iterator[FetchTask]: ...
    def fetch(self, task: FetchTask) -> Iterator[RawArtifact]: ...
