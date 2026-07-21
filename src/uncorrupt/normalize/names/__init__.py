"""Per-locale name normalization. Phase 1: interface + passthrough only (ADR-001 D4).

Per-locale ER golden sets gate enabling real normalization in a locale (Phase 2).
"""

from __future__ import annotations


def normalize_name(name: str, *, locale: str = "passthrough") -> str:
    return name.strip()
