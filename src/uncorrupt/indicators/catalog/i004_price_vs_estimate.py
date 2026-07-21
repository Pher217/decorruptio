"""Award value deviates sharply from the tender estimate (over/under-pricing)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from uncorrupt.indicators.base import Flag, Indicator, ValidationStatus
from uncorrupt.indicators.context import EvaluationContext


class PriceVsEstimate(Indicator):
    id = "i004_price_vs_estimate"
    title = "Price vs estimate"
    definition = "Award value deviates sharply from the tender estimate (over/under-pricing)."
    inputs: list[str] = ["tender.value", "awards.value"]
    params: dict[str, Any] = {"pct_threshold": 0.25}
    validation = {}  # UNVALIDATED everywhere until checked against local ground truth

    def evaluate(self, records: Any, ctx: EvaluationContext) -> Iterator[Flag]:
        raise NotImplementedError("Phase 1: implement against fixture OCDS releases")
