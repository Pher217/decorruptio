"""One supplier wins an outsized share of a buyer's awards over a window."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from uncorrupt.indicators.base import Flag, Indicator, ValidationStatus
from uncorrupt.indicators.context import EvaluationContext


class RepeatWinnerShare(Indicator):
    id = "i003_repeat_winner_share"
    title = "Repeat-winner share"
    definition = "One supplier wins an outsized share of a buyer's awards over a window."
    inputs: list[str] = ["awards", "parties"]
    params: dict[str, Any] = {"share_threshold": 0.5}
    validation = {}  # UNVALIDATED everywhere until checked against local ground truth

    def evaluate(self, records: Any, ctx: EvaluationContext) -> Iterator[Flag]:
        raise NotImplementedError("Phase 1: implement against fixture OCDS releases")
