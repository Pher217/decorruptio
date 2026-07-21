"""EU TED connector (Phase 1). OCDS via Kingfisher. data_class=A1.

Coverage honesty: TED is ABOVE-THRESHOLD notices only; most procurement by count
lives on national/below-threshold portals (ADR-001 / data-sources catalog).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

from uncorrupt.connectors.base import Connector, FetchTask, RawArtifact
from uncorrupt.core.tiers import DataClass


class EuTedConnector(Connector):
    source_id = "eu_ted"
    jurisdictions = ["EU"]
    data_class = DataClass.A1

    def discover(self, since: date | None = None) -> Iterator[FetchTask]:
        raise NotImplementedError("Phase 1: discover TED notice batches since `since`")

    def fetch(self, task: FetchTask) -> Iterator[RawArtifact]:
        raise NotImplementedError("Phase 1: fetch via Kingfisher TED spider")
