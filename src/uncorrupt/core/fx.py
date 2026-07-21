"""FX-at-date normalization (ADR-001 D4).

Amounts are normalized using the rate in effect on the event date, with the rate
source/type recorded in provenance. The original amount + currency are ALWAYS
preserved. Where a currency has multiple official rates or hyperinflation makes a
trustworthy rate unavailable, amount-based indicators are DISABLED rather than
computed on a bad number.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class FxProvenance:
    source: str  # e.g. "ecb"
    rate_type: str  # e.g. "reference", "official", "parallel"
    as_of: date
    rate: Decimal
    trusted: bool  # False => amount indicators must be disabled for this datum


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str  # ISO 4217


class FxRates:
    """Interface to a rate source. Phase 1 ships an ECB-backed implementation elsewhere."""

    def convert(self, money: Money, to: str, on: date) -> tuple[Money, FxProvenance]:
        raise NotImplementedError
