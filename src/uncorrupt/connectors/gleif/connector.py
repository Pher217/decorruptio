"""GLEIF LEI connector (Phase 1). Golden-copy download for the company-key overlay.

LEI is an enrichment overlay, not the primary company key (ADR-001 D4).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

from uncorrupt.connectors.base import Connector, FetchTask, RawArtifact
from uncorrupt.core.tiers import DataClass


class GleifConnector(Connector):
    source_id = "gleif"
    jurisdictions = ["GLOBAL"]
    data_class = DataClass.A1

    def discover(self, since: date | None = None) -> Iterator[FetchTask]:
        raise NotImplementedError("Phase 1: discover GLEIF golden-copy file")

    def fetch(self, task: FetchTask) -> Iterator[RawArtifact]:
        raise NotImplementedError("Phase 1: download GLEIF golden copy")
