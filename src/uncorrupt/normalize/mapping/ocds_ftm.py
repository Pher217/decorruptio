"""The single owned OCDS<->FtM mapping (ADR-001 D4 / spec §4). STUB — Phase 1 impl.

Reconciliation tests live in tests/mapping/.
"""

from __future__ import annotations

from typing import Any


def ocds_release_to_ftm(release: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one compiled OCDS release to FtM entity dicts. STUB."""
    raise NotImplementedError("Phase 1: map buyer/supplier/award to FtM entities")
