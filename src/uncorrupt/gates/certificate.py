"""ADR-008's "formal no-score certificate".

> "A failed readiness check emits an artifact naming exactly which controls
> blocked scoring, making 'we did not score' auditable rather than
> discretionary." -- ADR-008

This module builds that artifact from whatever this package's own
measurements found. It is deliberately independent of
`run_gold_benchmark.py`'s own (looser, out-of-scope) `CoverageGate.passed` /
`StratumGate.passed` -- a certificate emitted here can name a blocker
(e.g. a coverage gate at 92% accounted-for) even in a hypothetical future
where the downstream scorer's own 90%-ratio check would have silently
proceeded. That gap is intentional, not an oversight -- see
`coverage.CoverageMeasurement.to_gate_dict`'s docstring.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from uncorrupt.gates.binding import GateFreezeState
from uncorrupt.gates.coverage import CoverageMeasurement
from uncorrupt.gates.stratum import StratumMeasurement

CERTIFICATE_VERSION = 1
ADR_REFERENCE = "ADR-008-fail-closed-measurement-boundary"


@dataclass(frozen=True)
class Blocker:
    """One specific, named reason scoring cannot proceed."""

    gate: str
    reason: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"gate": self.gate, "reason": self.reason, "detail": self.detail}


def _coverage_blocker(name: str, measurement: CoverageMeasurement) -> Blocker | None:
    if measurement.passed:
        return None
    return Blocker(
        gate=f"coverage:{name}",
        reason=(
            f"{measurement.accounted_for}/{measurement.total} accounted for (spec A2.4.2 "
            f"requires 100%, not 90%) -- {measurement.not_attempted} record(s) never reached "
            "a terminal, auditable state"
        ),
        detail={
            "ingested": measurement.ingested,
            "explicitly_failed": measurement.explicitly_failed,
            "not_attempted": measurement.not_attempted,
            "total": measurement.total,
            "failure_manifest_sample": list(measurement.failure_manifest[:20]),
            "failure_manifest_truncated": len(measurement.failure_manifest) > 20,
            "known_limits": list(measurement.known_limits),
        },
    )


def _stratum_blocker(name: str, measurement: StratumMeasurement) -> Blocker | None:
    if not measurement.available:
        return Blocker(
            gate=f"stratum:{name}",
            reason="no external control battery available for this stratum (spec A2.4.3)",
            detail={"note": measurement.note},
        )
    if not measurement.passed:
        return Blocker(
            gate=f"stratum:{name}",
            reason=(
                f"retrieval_passed={measurement.retrieval_passed} "
                f"temporal_passed={measurement.temporal_passed} -- both required for a "
                "passing stratum gate (spec A2.4.4)"
            ),
            detail={
                "retrieval_recovered": measurement.retrieval_recovered,
                "retrieval_total": measurement.retrieval_total,
                "temporal_recovered": measurement.temporal_recovered,
                "temporal_total": measurement.temporal_total,
                "note": measurement.note,
            },
        )
    return None


def build_no_score_certificate(
    freeze_state: GateFreezeState,
    coverage_measurements: dict[str, CoverageMeasurement] | None = None,
    stratum_measurements: dict[str, StratumMeasurement] | None = None,
) -> dict[str, Any] | None:
    """A no-score certificate dict if ANY measured gate fails, else `None`.

    `coverage_measurements` / `stratum_measurements`: pass whichever this
    caller measured -- `scripts/measure_coverage_gate.py` passes only
    coverage, `scripts/measure_stratum_gates.py` passes only stratum. Either
    half alone is a legitimate, independently blocking certificate; passing
    both produces one combined artifact naming every blocker found.

    Returns `None` (no certificate) only when every measurement passed --
    an empty `coverage_measurements`/`stratum_measurements` produces no
    blockers and therefore no certificate, which is correct: a caller that
    measured nothing has nothing to report as blocking, that is not the
    same claim as "everything passed".
    """
    blockers: list[Blocker] = []
    for name, measurement in (coverage_measurements or {}).items():
        blocker = _coverage_blocker(name, measurement)
        if blocker is not None:
            blockers.append(blocker)
    for name, measurement in (stratum_measurements or {}).items():
        blocker = _stratum_blocker(name, measurement)
        if blocker is not None:
            blockers.append(blocker)

    if not blockers:
        return None

    return {
        "no_score": True,
        "certificate_version": CERTIFICATE_VERSION,
        "issued_at": datetime.now(UTC).isoformat(),
        "freeze_state": freeze_state.to_binding_dict(),
        "blockers": [b.to_dict() for b in blockers],
        "adr": ADR_REFERENCE,
        "note": (
            "This certificate exists so 'we did not score' is auditable rather than "
            "discretionary (ADR-008). Every gate named in `blockers` independently blocks "
            "scoring -- fixing one does not imply the others are also fixed."
        ),
    }


def write_no_score_certificate(path: str | Path, certificate: dict[str, Any]) -> Path:
    """Write a certificate dict (from `build_no_score_certificate`) to disk."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(certificate, indent=2), encoding="utf-8")
    return path
