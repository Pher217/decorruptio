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
from collections.abc import Iterable
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


def _unmeasured_blocker(gate: str, reason: str) -> Blocker:
    """A required gate family that never reached a real measurement this
    run -- UNMEASURED is itself a blocker, never an omission and never a
    silent pass (ADR-008: "we did not measure this" is not the same claim
    as "this passed"). Distinct in wording from a measured FAIL so a reader
    can tell "nobody ran the instrument this time" apart from "the
    instrument ran and found a shortfall".
    """
    return Blocker(gate=gate, reason=f"UNMEASURED: {reason}", detail={"status": "UNMEASURED"})


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
    unmeasured_families: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """A no-score certificate dict if ANY measured gate fails or any
    required family was never measured, else `None`.

    `coverage_measurements` / `stratum_measurements`: pass whichever this
    caller measured -- `scripts/measure_coverage_gate.py` passes only
    coverage, `scripts/measure_stratum_gates.py` passes only stratum. Either
    half alone is a legitimate, independently blocking certificate; passing
    both produces one combined artifact naming every blocker found.

    `unmeasured_families`: gate names (already prefixed, e.g.
    `"coverage:commons_register"`) this caller knows are REQUIRED for its
    certificate but did not reach a real measurement this run -- a report
    file that does not exist, is unbound to the current freeze state, or is
    missing an expected entry. Every one of these is added as an
    UNMEASURED blocker unconditionally: unknown must never be read as
    passing (ADR-008). This is how a caller closes the fail-closed hole a
    missing family would otherwise leave -- see
    `assert_all_required_families_accounted_for` for the accompanying
    structural guarantee that a caller forgot to do this at all.

    Returns `None` (no certificate) only when every measurement passed and
    no family was flagged unmeasured -- an empty `coverage_measurements`/
    `stratum_measurements`/`unmeasured_families` produces no blockers and
    therefore no certificate, which is correct: a caller that measured
    nothing AND declared nothing required has nothing to report as
    blocking, that is not the same claim as "everything passed".
    """
    blockers: list[Blocker] = []
    for name, coverage_measurement in (coverage_measurements or {}).items():
        blocker = _coverage_blocker(name, coverage_measurement)
        if blocker is not None:
            blockers.append(blocker)
    for name, stratum_measurement in (stratum_measurements or {}).items():
        blocker = _stratum_blocker(name, stratum_measurement)
        if blocker is not None:
            blockers.append(blocker)
    for gate, reason in (unmeasured_families or {}).items():
        blockers.append(_unmeasured_blocker(gate, reason))

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


def assert_all_required_families_accounted_for(
    required_gate_names: Iterable[str],
    certificate: dict[str, Any] | None,
    passed_families: Iterable[str] = (),
) -> None:
    """Refuse to let a caller compute a verdict while any name in
    `required_gate_names` was never evaluated as PASS, FAIL, or UNMEASURED.

    This is the structural guarantee closing the certificate's fail-closed
    hole: a certificate (or a caller's decision that none was needed) must
    never rest on a gate family that was simply never evaluated. A family
    the caller never measured and never declared unmeasured is worse than
    one honestly marked UNMEASURED -- ADR-008 requires "we did not evaluate
    this" to itself be an auditable, blocking fact, never a silent gap that
    reads as a pass.

    A family is accounted for when it names a blocker in
    `certificate["blockers"]` (a FAIL or an UNMEASURED blocker -- both are
    blockers, see `_coverage_blocker`/`_stratum_blocker`/
    `_unmeasured_blocker`) OR is listed in `passed_families` -- the
    caller's own record that this family was measured AND passed, which is
    exactly why it produced no blocker. `certificate` may be `None`
    (`build_no_score_certificate`'s contract: every measured family
    passed and none was declared unmeasured) -- in that case every
    required family must appear in `passed_families`, or this still
    raises, because `None` alone does not prove which families were
    actually evaluated.

    Raises `ValueError` naming exactly which families are missing --
    never silently proceeds. Callers invoke this immediately before
    deciding a verdict or writing a certificate to disk.
    """
    blocker_gates = {b["gate"] for b in (certificate or {}).get("blockers", [])}
    accounted = blocker_gates | set(passed_families)
    missing = sorted(set(required_gate_names) - accounted)
    if missing:
        raise ValueError(
            f"refusing to compute a verdict: {missing} were never evaluated as PASS, FAIL, "
            "or UNMEASURED for this certificate -- every required gate family must be "
            "explicitly accounted for (ADR-008); 'not evaluated' must never silently pass."
        )
