"""Adapter to drive OCP Kingfisher Collect spiders and harvest RawArtifacts.

Kingfisher Collect is a Scrapy project run as a *tool*, not imported as a library
(ADR-001 D3 / spec §4). Phase 1: thin subprocess driver. STUB body.
"""

from __future__ import annotations

from collections.abc import Iterator

from uncorrupt.connectors.base import RawArtifact


def run_spider(spider: str, **opts: str) -> Iterator[RawArtifact]:
    """Run a Kingfisher spider and yield raw OCDS payloads. STUB — Phase 1 impl."""
    raise NotImplementedError("Kingfisher adapter is implemented in Phase 1")
