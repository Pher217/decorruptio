"""High share of contracts let by direct/non-competitive procedure."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from uncorrupt.indicators.base import Flag, Indicator, ValidationStatus
from uncorrupt.indicators.context import EvaluationContext


class DirectAwardShare(Indicator):
    id = "i005_direct_award_share"
    title = "Direct-award share"
    definition = "High share of contracts let by direct/non-competitive procedure."
    inputs: list[str] = ["tender.procurementMethod"]
    params: dict[str, Any] = {"share_threshold": 0.3}
    validation = {}  # UNVALIDATED everywhere until checked against local ground truth

    def evaluate(self, records: Any, ctx: EvaluationContext) -> Iterator[Flag]:
        raise NotImplementedError("Phase 1: implement against fixture OCDS releases")
