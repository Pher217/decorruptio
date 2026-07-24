"""EvaluationContext — passed to every indicator so bitemporal + locale + FX trust
are arguments, not something each indicator reinvents (ADR-001 D4)."""

from __future__ import annotations

from dataclasses import dataclass

from uncorrupt.register.models import LocaleProfile


@dataclass(frozen=True)
class EvaluationContext:
    locale: LocaleProfile
    source_id: str  # the data source to evaluate (filters staging queries)
    # As-of semantics: compare date-only in source-local civil time (ADR-001 D4).
    fx_trusted: bool = True
    # When False, amount-based indicators must abstain rather than emit a flag.
