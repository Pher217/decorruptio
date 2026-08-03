"""Spec A2.4.3 per-material-stratum retrieval/temporal gate measurement.

Unlike `coverage.py` (where the independent review ruled 90% indefensible
for census-style ingestion), the 90% figure used here IS the correct
standard: `A2.4.3`'s ">=9/10 per material stratum" describes a
PRE-REGISTERED EXTERNAL CONTROL SAMPLE -- exactly the case the review said
90% remains defensible for. `StratumMeasurement.retrieval_passed`/
`temporal_passed` therefore mirror `run_gold_benchmark.StratumGate`'s own
>=90% computation deliberately, not by oversight.

Only ONE stratum currently has a pre-registered external control fixture in
this repository: Lords (`tests/fixtures/lords_retrieval_controls.json`,
spec v2.9). Companies House and Commons have none yet -- sourcing one is a
labour-intensive fact-gathering task (assembling independently verified
peer/company or member/company pairs), not a measurement-tooling gap, and
is out of this deliverable's scope. Those strata are therefore reported
`available=False` -- fail closed (ADR-008: "a missing input is an error,
never a default pass"), never guessed at.

Electoral Commission is measured and reported here even though it is NOT
one of `run_gold_benchmark.MATERIAL_STRATA` -- see
`donation_edges_are_ungated_in_scorer`'s docstring for why that gap matters
and cannot be closed from this package alone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTROLS_PASS_FRACTION = 0.9  # matches run_gold_benchmark.CONTROLS_PASS_FRACTION deliberately

DEFAULT_LORDS_CONTROLS_PATH = "tests/fixtures/lords_retrieval_controls.json"
DEFAULT_CH_CONTROLS_PATH = "tests/fixtures/ch_temporal_controls.json"
DEFAULT_COMMONS_CONTROLS_PATH = "tests/fixtures/commons_retrieval_controls.json"
DEFAULT_EC_CONTROLS_PATH = "tests/fixtures/ec_retrieval_controls.json"

STRATUM_COMMONS = "commons_declared_interest"
STRATUM_LORDS = "lords_declared_interest"
STRATUM_CH_OFFICER = "ch_officer_appointment"
STRATUM_ELECTORAL_COMMISSION = "electoral_commission"  # NOT in run_gold_benchmark.MATERIAL_STRATA


@dataclass(frozen=True)
class StratumMeasurement:
    """One material stratum's measured retrieval/temporal state.

    Field names and pass semantics mirror `run_gold_benchmark.StratumGate`
    exactly (`available`, `retrieval_recovered`, `retrieval_total`,
    `temporal_recovered`, `temporal_total`) so `to_gate_dict()` round-trips
    through `load_stratum_gates` unchanged. `available=False` (the default)
    means no external gating control exists for this stratum at all -- it
    can never pass, matching `StratumGate`'s own documented contract.
    """

    name: str
    available: bool = False
    retrieval_recovered: int | None = None
    retrieval_total: int | None = None
    temporal_recovered: int | None = None
    temporal_total: int | None = None
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def retrieval_passed(self) -> bool:
        return (
            self.retrieval_total is not None
            and self.retrieval_total > 0
            and self.retrieval_recovered is not None
            and 0 <= self.retrieval_recovered <= self.retrieval_total
            and (self.retrieval_recovered / self.retrieval_total) >= CONTROLS_PASS_FRACTION
        )

    @property
    def temporal_passed(self) -> bool:
        return (
            self.temporal_total is not None
            and self.temporal_total > 0
            and self.temporal_recovered is not None
            and 0 <= self.temporal_recovered <= self.temporal_total
            and (self.temporal_recovered / self.temporal_total) >= CONTROLS_PASS_FRACTION
        )

    @property
    def passed(self) -> bool:
        return self.available and self.retrieval_passed and self.temporal_passed

    def to_gate_dict(self) -> dict[str, Any]:
        """The fields `run_gold_benchmark.load_stratum_gates` reads."""
        return {
            "available": self.available,
            "retrieval_recovered": self.retrieval_recovered,
            "retrieval_total": self.retrieval_total,
            "temporal_recovered": self.temporal_recovered,
            "temporal_total": self.temporal_total,
        }


def measure_lords_stratum(
    controls_path: str | Path = DEFAULT_LORDS_CONTROLS_PATH,
    max_hops: int = 2,
) -> StratumMeasurement:
    """Lords retrieval control (spec v2.9: retrieval closed, temporal never available).

    Reuses `scripts.run_lords_controls.run_controls()` (read-only -- queries
    the graph, writes nothing) against the fixed 12-row control battery. Per
    v2.9/A2.9.2, and enforced by `run_lords_controls.py`'s own fixed
    `TEMPORAL_ENDPOINT_STATUS` constant (never computed from row content),
    temporal fields are left `None` here -- not `0/0`, not any measured
    ratio -- so `temporal_passed` is unconditionally `False` regardless of
    retrieval performance now or later. This is the binding requirement
    "Lords temporal never reports a pass": there is no combination of
    `retrieval_recovered`/`retrieval_total` that can flip it, because
    `temporal_passed` never reads them.
    """
    from scripts.run_lords_controls import run_controls

    result = run_controls(controls_path=controls_path, max_hops=max_hops)
    return StratumMeasurement(
        name=STRATUM_LORDS,
        available=True,
        retrieval_recovered=result["recovered"],
        retrieval_total=result["n"],
        temporal_recovered=None,
        temporal_total=None,
        note=(
            "retrieval measured via scripts/run_lords_controls.py (spec v2.9 -- the "
            "Cloudflare response was a browser bot-challenge, not an IP block; a "
            "real-browser-captured frozen snapshot backs a 12-row control battery). Temporal "
            "is intentionally left unmeasured (None, not 0) -- the register publishes no "
            "interest start dates and the earliest available snapshot postdates most award "
            "dates (spec A2.5.1) -- temporal_passed is unconditionally False, never a pass."
        ),
        extra={"unresolved": result["unresolved"], "not_found": result["not_found"]},
    )


def _external_control_stratum(
    name: str, controls_path: str | Path | None, note_when_missing: str
) -> StratumMeasurement:
    """Fail-closed default for a stratum with no external control fixture
    wired up yet -- unavailable, never a default pass (ADR-008: "a missing
    input is an error, never a default pass")."""
    if controls_path is None:
        return StratumMeasurement(name=name, available=False, note=note_when_missing)
    path = Path(controls_path)
    if not path.exists():
        return StratumMeasurement(
            name=name,
            available=False,
            note=f"{note_when_missing} (looked for a fixture at {path}, not found).",
        )
    # A fixture file exists at the given path, but this package does not yet
    # define a control-battery RUNNER for this stratum (only Lords has one:
    # scripts/run_lords_controls.py). Building one is a sourcing task
    # (assembling independently verified external facts), out of this
    # tooling deliverable's scope -- reported unavailable, never guessed at.
    return StratumMeasurement(
        name=name,
        available=False,
        note=(
            f"a control fixture exists at {path}, but no control-battery runner is wired up "
            f"for {name} yet (only Lords has one -- scripts/run_lords_controls.py). Building "
            "one requires sourcing independently verified external facts, out of this "
            "measurement-tooling deliverable's scope; reported unavailable rather than "
            "guessed at."
        ),
    )


def measure_ch_officer_stratum(
    controls_path: str | Path | None = DEFAULT_CH_CONTROLS_PATH,
) -> StratumMeasurement:
    """Companies House officer/appointment retrieval + temporal (spec A2.4.3).

    92.3% of `officer_of` edges carry `appointed_on` (packet) -- temporal is
    MEASURABLE in principle, but that figure describes date AVAILABILITY,
    not a passing external control. No pre-registered external CH temporal
    control fixture exists yet in this repository (unlike Lords). Fail
    closed: unavailable until one does -- spec v2.7 §A2.7.4 names exactly
    this ("a pre-frozen external CH temporal control set end-to-end") as
    the single most informative next measurement, not yet built.
    """
    return _external_control_stratum(
        STRATUM_CH_OFFICER,
        controls_path,
        "no external Companies House temporal control battery exists yet (spec v2.7 "
        "§A2.7.4's own next action) -- 92.3% appointed_on coverage measures date "
        "AVAILABILITY, not a passing control.",
    )


def measure_commons_stratum(
    controls_path: str | Path | None = DEFAULT_COMMONS_CONTROLS_PATH,
) -> StratumMeasurement:
    """Commons `declared_interest` retrieval + temporal (spec A2.4.3).

    ~95% of API records carry `registrationDate` (packet) -- dateable in
    principle -- but the source is severely under-ingested (see
    `coverage.measure_commons_coverage`) and no external Commons control
    fixture exists yet. Fail closed: unavailable.
    """
    return _external_control_stratum(
        STRATUM_COMMONS,
        controls_path,
        "no external Commons retrieval/temporal control battery exists yet -- the 10 "
        "externally-sourced Commons controls referenced in spec A2.4.3 have no fixture file "
        "in this repository (only tests/fixtures/lords_retrieval_controls.json exists).",
    )


def measure_electoral_commission_stratum(
    controls_path: str | Path | None = DEFAULT_EC_CONTROLS_PATH,
) -> StratumMeasurement:
    """Electoral Commission donation-edge retrieval + temporal.

    NOT one of `run_gold_benchmark.MATERIAL_STRATA` (exactly three entries:
    commons_declared_interest, lords_declared_interest,
    ch_officer_appointment) -- `classify_edge_stratum` there returns `None`
    for `donation` edges by design ("non-material edge types ... return
    None"). This module still measures and reports it: the sealed cohort's
    own case list includes cases whose evidence stratum is
    `electoral_commission` (e.g. `01428210`, `SC149147`, and the mixed
    `12597000`/`08126173`), and `donation_edges_are_ungated_in_scorer`
    documents the precise mechanism by which that matters. Reported here so
    the no-score certificate (`certificate.py`) can name it, and so whoever
    next amends `run_gold_benchmark.MATERIAL_STRATA` has a ready producer --
    never silently swallowed.
    """
    return _external_control_stratum(
        STRATUM_ELECTORAL_COMMISSION,
        controls_path,
        "no external Electoral Commission control battery exists yet, AND this stratum is "
        "not in run_gold_benchmark.MATERIAL_STRATA at all -- a passing gate here would "
        "currently have no effect on the scorer's verdict logic either way (see "
        "donation_edges_are_ungated_in_scorer).",
    )


def donation_edges_are_ungated_in_scorer() -> bool:
    """True if the scorer (owned elsewhere, not edited here) has no material
    stratum for `donation` edges at all -- the precise mechanism the
    delegation packet flagged.

    `run_gold_benchmark.classify_edge_stratum` returns `None` for a
    `donation` edge, and `path_strata()` builds a path's reported strata by
    filtering out every `None` -- so a MIXED path (e.g. one `officer_of`
    edge plus one `donation` edge) reports strata `{ch_officer_appointment}`
    only, silently omitting that the donation edge contributed evidence too.
    If the Companies House stratum is passing, that mixed path can qualify
    for CONFIRMED/PARTIAL through the CH gate alone -- with the donation
    edge's own evidentiary contribution completely unvalidated by any
    control. A pure donation-only path cannot false-qualify this way (an
    empty strata set fails `PathEvidence.passes_stratum_gates`'s
    `bool(self.strata)` check) -- the real risk is specifically the mixed
    case, which the sealed cohort's own case list contains (`12597000`,
    `08126173`).

    Imports `scripts.run_gold_benchmark` read-only (never edited) purely to
    check its own published `MATERIAL_STRATA` constant -- if that module is
    later amended to add an `electoral_commission` stratum, this check (and
    the no-score certificate note it feeds) becomes a no-op automatically,
    never a stale warning.
    """
    from scripts.run_gold_benchmark import MATERIAL_STRATA

    return STRATUM_ELECTORAL_COMMISSION not in MATERIAL_STRATA


def measure_all_strata(
    lords_controls_path: str | Path = DEFAULT_LORDS_CONTROLS_PATH,
    ch_controls_path: str | Path | None = DEFAULT_CH_CONTROLS_PATH,
    commons_controls_path: str | Path | None = DEFAULT_COMMONS_CONTROLS_PATH,
    ec_controls_path: str | Path | None = DEFAULT_EC_CONTROLS_PATH,
    max_hops: int = 2,
) -> dict[str, StratumMeasurement]:
    """All four strata this package measures -- the three
    `run_gold_benchmark.MATERIAL_STRATA` plus the extra, currently-ungated
    `electoral_commission` (see `donation_edges_are_ungated_in_scorer`)."""
    return {
        STRATUM_COMMONS: measure_commons_stratum(commons_controls_path),
        STRATUM_LORDS: measure_lords_stratum(lords_controls_path, max_hops=max_hops),
        STRATUM_CH_OFFICER: measure_ch_officer_stratum(ch_controls_path),
        STRATUM_ELECTORAL_COMMISSION: measure_electoral_commission_stratum(ec_controls_path),
    }
