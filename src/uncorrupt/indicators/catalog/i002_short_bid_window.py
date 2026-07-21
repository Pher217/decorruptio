"""Bid submission window shorter than the legal/typical minimum (OCP)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from uncorrupt.indicators.base import Flag, Indicator
from uncorrupt.indicators.context import EvaluationContext


class ShortBidWindow(Indicator):
    id = "i002_short_bid_window"
    title = "Short bid window"
    definition = "Bid submission window shorter than the legal/typical minimum (OCP)."
    inputs: list[str] = ["tender.tenderPeriod"]
    params: dict[str, Any] = {"min_days": 15}
    validation = {}  # UNVALIDATED everywhere until checked against local ground truth

    def evaluate(self, records: Any, ctx: EvaluationContext) -> Iterator[Flag]:
        raise NotImplementedError("Phase 1: implement against fixture OCDS releases")
