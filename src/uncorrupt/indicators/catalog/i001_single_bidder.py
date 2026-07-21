"""Single-bidder award: only one valid bid received (World Bank / OCP i001)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from uncorrupt.indicators.base import Flag, Indicator, ValidationStatus
from uncorrupt.indicators.context import EvaluationContext


class SingleBidder(Indicator):
    id = "i001_single_bidder"
    title = "Single bidder"
    definition = "Single-bidder award: only one valid bid received (World Bank / OCP i001)."
    inputs: list[str] = ["awards", "bids"]
    params: dict[str, Any] = {}
    validation = {}  # UNVALIDATED everywhere until checked against local ground truth

    def evaluate(self, records: Any, ctx: EvaluationContext) -> Iterator[Flag]:
        raise NotImplementedError("Phase 1: implement against fixture OCDS releases")
