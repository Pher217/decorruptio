"""Data classification and publication tiers — the guardrails, made typed.

- DataClass: A1 (non-personal), A2 (public-persons), B (flagging). See ADR-001 D2.
- Tier: publication tier a (open) / b (vetted) / c (named). See ADR-000 G5.

Field-level tier tagging + the export gate live here so ADR-000 G2 is enforced in
code, not just prose.
"""

from __future__ import annotations

from enum import StrEnum

from uncorrupt.core.errors import TierViolation


class DataClass(StrEnum):
    A1 = "A1"  # non-personal money data — scales freely
    A2 = "A2"  # public-persons data — gated by up-front global DPIA
    B = "B"  # flagging & investigation — gated per-jurisdiction


class Tier(StrEnum):
    A = "a"  # open: raw open data + reproducible aggregations
    B = "b"  # vetted: anomaly feed to auditors/journalists under a code of conduct
    C = "c"  # named: human investigation + right of reply + legal review only


# Which tiers each publication target accepts.
_OPEN_EXPORT_TIERS = {Tier.A}


def assert_exportable_to_open(tier: Tier, *, what: str = "record") -> None:
    """Raise unless `tier` may appear in a tier-a (open) export (ADR-000 G2)."""
    if tier not in _OPEN_EXPORT_TIERS:
        raise TierViolation(f"{what} is tier '{tier.value}' and cannot reach a tier-a open export")
