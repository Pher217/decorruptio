"""Indicator interface — the "risk indicator, never verdict" contract in code (ADR-000 G4).

Every Flag is explainable (human-readable) and carries source-doc evidence + a
version stamp. Indicators are disabled-until-validated per jurisdiction: an
indicator with no VALIDATED status for a locale will not run there (the Italian
Decarolis-Giorgiantonio finding: intuitive indicators can be worthless).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from uncorrupt.core.provenance import ProvenanceRecord, VersionStamp
from uncorrupt.indicators.context import EvaluationContext


class ValidationStatus(StrEnum):
    UNVALIDATED = "unvalidated"  # default — will NOT run
    VALIDATED = "validated"  # checked against local ground truth — may run
    REJECTED = "rejected"  # found not predictive here — must not run


@dataclass(frozen=True)
class Flag:
    indicator_id: str
    subject_ref: str  # e.g. an OCDS ocid or FtM entity id
    as_of: date
    explanation: str  # human-readable — mandatory
    evidence: list[ProvenanceRecord]  # source-doc back-references — mandatory
    stamp: VersionStamp


class Indicator(ABC):
    id: str
    title: str
    definition: str  # human-readable formula, cited to OCP-73 / World Bank id
    inputs: list[str]  # OCDS paths consumed
    params: dict[str, Any]
    validation: dict[str, ValidationStatus]  # keyed by jurisdiction/locale code

    # Set by evaluate() to the number of units (tenders, buyers, pairs)
    # actually evaluated for the source. Used by the runner to compute
    # base rates: flags_emitted / units_evaluated.
    units_evaluated: int = 0

    def runs_in(self, locale_code: str) -> bool:
        return self.validation.get(locale_code) is ValidationStatus.VALIDATED

    @abstractmethod
    def evaluate(self, ctx: EvaluationContext) -> Iterator[Flag]:
        """Yield Flags. Queries Django staging models directly.

        Must set self.units_evaluated to the count of units iterated
        (tenders, buyers, or pairs — the indicator's real unit).
        """
