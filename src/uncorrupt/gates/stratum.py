"""Spec A2.4.3 per-material-stratum retrieval/temporal gate measurement.

Unlike `coverage.py` (where the independent review ruled 90% indefensible
for census-style ingestion), the 90% figure used here IS the correct
standard: `A2.4.3`'s ">=9/10 per material stratum" describes a
PRE-REGISTERED EXTERNAL CONTROL SAMPLE -- exactly the case the review said
90% remains defensible for. `StratumMeasurement.retrieval_passed`/
`temporal_passed` therefore mirror `run_gold_benchmark.StratumGate`'s own
>=90% computation deliberately, not by oversight.

All four strata now have a pre-registered external control fixture AND a
wired control-battery runner: Lords (`tests/fixtures/lords_retrieval_controls
.json`, `scripts/run_lords_controls.py`, spec v2.9), Companies House
(`tests/fixtures/ch_temporal_controls.json`, `scripts/run_ch_controls.py`),
Commons (`tests/fixtures/commons_retrieval_controls.json`,
`scripts/run_commons_controls.py`), and Electoral Commission
(`tests/fixtures/ec_retrieval_controls.json`, `scripts/run_ec_controls.py`).
Each runner is REUSED here (imported, never reimplemented) via
`_measure_wired_stratum` -- fail closed (ADR-008: "a missing input is an
error, never a default pass") whenever the runner cannot execute at all: no
`controls_path` configured, no fixture file at that path, or the runner
itself raises (malformed fixture, DB unreachable, or any other runner
error). A fixture existing alone is never sufficient -- wiring the runner is
the deliberate act that makes a stratum `available`, and a runner that DOES
execute but measures a real, low score reports `available=True` with
`passed=False`, never `unavailable` and never a silently-defaulted zero.

Retrieval and temporal are reported as two DISTINCT figures, never
conflated (mirrors each runner's own module docstring): `temporal_total` is
the runner's own raw battery size (`result["temporal_total"]`, always equal
to `n`/`retrieval_total`) -- NOT rescaled to the retrieved subset. An
earlier version of this module rescaled `temporal_total` to
`retrieval_recovered`, which created a perverse gradient an independent
review demonstrated by mutation: corrupting a single already-matching date
in a real 12-row fixture shifted the reported ratio from 11/11 to 10/11
(still >=90%, still PASS) instead of the runner's own honest 10/12 (<90%,
FAIL) -- worse retrieval SHRANK the temporal denominator and made temporal
easier to pass, exactly backwards. Sharing the denominator with retrieval
closes that: a control that fails to retrieve can never earn a temporal
credit (`run_*_controls.py`'s own `TEMPORAL_NOT_APPLICABLE` status already
guarantees `temporal_recovered` cannot exceed `retrieval_recovered`), and it
correctly costs the temporal ratio too, not just retrieval's. Lords is the
sole structural exception: the register publishes no interest start dates
at all, so `measure_lords_stratum` leaves `temporal_recovered`/
`temporal_total` permanently `None` (see that function's own docstring) --
`temporal_passed` can never flip to `True` for Lords regardless of
retrieval performance.

`_measure_wired_stratum` also refuses `available=True` for any control
battery smaller than `MIN_CONTROL_BATTERY_SIZE` (12, the pre-registered
size of every real fixture in this repository) -- without this floor, the
same independent review demonstrated that pointing `--ch-controls` (or
`--commons-controls`/`--ec-controls`, all free CLI paths in
`scripts/measure_stratum_gates.py`) at an arbitrary 1-row fixture where that
one row happens to resolve reports `available=True, passed=True`, even
though the real 12-row battery for that same stratum scores 6/12 and FAILS.
Both `ch_officer_appointment` and `commons_declared_interest` are in
`run_gold_benchmark.MATERIAL_STRATA`, gating both REFUTED
(`all_strata_pass`) and CONFIRMED/PARTIAL (`passes_stratum_gates`) --
before this module existed, both were hard-coded `available=False`, so no
fixture at all could unlock them; the floor restores an equivalent
guarantee now that a fixture path is caller-controlled.

Because `GateFreezeState` (`uncorrupt.gates.binding`) binds `code_commit`/
`graph_hash`/`attestation_inclusive_hash`/`manifest_hash` -- never the
control fixture's own content -- an edited-but-uncommitted fixture changes
none of those and still satisfies both `GateBinding.matches()` (the
scorer's own check) and `GateFreezeState.matches_recorded()` (this
package's stricter one). `compute_control_fixtures_hash()` below closes
that visibility gap: `scripts/measure_stratum_gates.py` folds its result
into `GateFreezeState.control_fixtures_hash`, so an artifact records
exactly which fixture bytes produced its numbers, auditable independently
of `MIN_CONTROL_BATTERY_SIZE`'s size-based enforcement.

Electoral Commission is measured and reported here even though it is NOT
one of `run_gold_benchmark.MATERIAL_STRATA` -- see
`donation_edges_are_ungated_in_scorer`'s docstring for why that gap matters
and cannot be closed from this package alone.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTROLS_PASS_FRACTION = 0.9  # matches run_gold_benchmark.CONTROLS_PASS_FRACTION deliberately

# The pre-registered size of every real control-battery fixture in this
# repository (Lords/CH/Commons/EC all ship exactly 12 rows). A wired stratum
# refuses `available=True` below this floor -- see module docstring's
# "refuses available=True for any control battery smaller than
# MIN_CONTROL_BATTERY_SIZE" section for the exploit this closes.
MIN_CONTROL_BATTERY_SIZE = 12

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


def _measure_wired_stratum(
    name: str,
    controls_path: str | Path | None,
    run_controls_fn: Callable[..., dict[str, Any]],
    note_when_missing: str,
    runner_label: str,
) -> StratumMeasurement:
    """Fail-closed wiring for a stratum backed by an external control-battery
    runner (`scripts/run_*_controls.py`'s own `run_controls`, REUSED here,
    never reimplemented -- see module docstring).

    Unavailable -- never `passing`, never a silently-defaulted zero -- in
    every case where the runner cannot execute at all (ADR-008: "a missing
    input is an error, never a default pass"):
      * no `controls_path` configured (`None`);
      * no fixture file exists at the configured path;
      * the runner itself raises -- a malformed fixture, an unreachable DB,
        or any other runner error. The exception is captured into `note`
        for diagnosis, never swallowed silently.

    Once the runner DOES execute against a battery at least
    `MIN_CONTROL_BATTERY_SIZE` rows, `available=True` unconditionally -- a
    low or zero score is a real, informative measurement (`passed=False`),
    not a reason to report unavailable (mirrors `measure_lords_stratum`'s
    own precedent: `available=True` regardless of the retrieval count). A
    battery SMALLER than that floor also reports unavailable -- an
    executed-but-undersized battery is not a passing (or failing) score,
    it is an untrusted input (see module docstring for the exploit this
    closes: an arbitrary 1-row `--ch-controls` fixture could otherwise
    report `available=True, passed=True`).

    `temporal_total` is the runner's own raw `result["temporal_total"]`
    (always `n`, the same as `retrieval_total`), never rescaled -- see
    module docstring's "Retrieval and temporal are reported as two DISTINCT
    figures" section for the perverse-gradient exploit rescaling to
    `retrieval_recovered` created.
    """
    if controls_path is None:
        return StratumMeasurement(name=name, available=False, note=note_when_missing)
    path = Path(controls_path)
    if not path.exists():
        return StratumMeasurement(
            name=name,
            available=False,
            note=f"{note_when_missing} (looked for a fixture at {path}, not found).",
        )
    try:
        fixture_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        result = run_controls_fn(controls_path=path)
    except Exception as exc:  # noqa: BLE001 -- fail-closed by design, see docstring above
        return StratumMeasurement(
            name=name,
            available=False,
            note=(
                f"{runner_label} raised {exc!r} while running the control battery at {path} -- "
                "reported unavailable, never a silent zero and never passing (ADR-008)."
            ),
            extra={"fixture_path": str(path)},
        )

    retrieval_recovered = result["retrieval_recovered"]
    retrieval_total = result["retrieval_total"]
    temporal_recovered = result["temporal_recovered"]
    temporal_total = result["temporal_total"]
    battery_extra = {
        **{key: result[key] for key in ("n", "not_found", "unresolved") if key in result},
        "fixture_path": str(path),
        "fixture_sha256": fixture_sha256,
        "battery_size": retrieval_total,
    }

    if retrieval_total < MIN_CONTROL_BATTERY_SIZE:
        return StratumMeasurement(
            name=name,
            available=False,
            note=(
                f"{runner_label} ran against a {retrieval_total}-row control battery at "
                f"{path}, below the pre-registered {MIN_CONTROL_BATTERY_SIZE}-row battery "
                "size -- refusing available=True for an arbitrarily small or substituted "
                "fixture (a small battery can trivially clear the >=9/10 bar; only the "
                "pre-registered battery size is trusted, ADR-008)."
            ),
            extra=battery_extra,
        )

    return StratumMeasurement(
        name=name,
        available=True,
        retrieval_recovered=retrieval_recovered,
        retrieval_total=retrieval_total,
        temporal_recovered=temporal_recovered,
        temporal_total=temporal_total,
        note=(
            f"retrieval + temporal measured live via {runner_label} against fixture {path} "
            f"({retrieval_total} controls, sha256={fixture_sha256[:12]}...). temporal_total "
            "is the runner's own raw battery size, never rescaled (see module docstring)."
        ),
        extra=battery_extra,
    )


def measure_ch_officer_stratum(
    controls_path: str | Path | None = DEFAULT_CH_CONTROLS_PATH,
) -> StratumMeasurement:
    """Companies House officer/appointment retrieval + temporal (spec A2.4.3).

    92.3% of `officer_of` edges carry `appointed_on` (packet) -- temporal
    IS measurable, and `scripts/run_ch_controls.py` (REUSED here, not
    reimplemented) now closes spec v2.7 §A2.7.4's own next action ("a
    pre-frozen external CH temporal control set end-to-end") with a 12-row
    fixture of real (officer_id, company_number, appointed_on) triples
    fetched live from the Companies House REST API.
    """
    from scripts.run_ch_controls import run_controls

    return _measure_wired_stratum(
        STRATUM_CH_OFFICER,
        controls_path,
        run_controls,
        "no external Companies House temporal control battery exists yet (spec v2.7 "
        "§A2.7.4's own next action) -- 92.3% appointed_on coverage measures date "
        "AVAILABILITY, not a passing control.",
        "scripts/run_ch_controls.py",
    )


def measure_commons_stratum(
    controls_path: str | Path | None = DEFAULT_COMMONS_CONTROLS_PATH,
) -> StratumMeasurement:
    """Commons `declared_interest` retrieval + temporal (spec A2.4.3).

    ~95% of API records carry `registrationDate` (packet) -- dateable in
    principle, but the source is severely under-ingested (see
    `coverage.measure_commons_coverage`). `scripts/run_commons_controls.py`
    (REUSED here) closes the fixture gap with a 12-row externally-sourced
    control battery; MOST rows are EXPECTED to classify not-found -- that is
    the correct, informative result of a genuine coverage gap, not a
    control-battery defect (see that script's own module docstring).
    """
    from scripts.run_commons_controls import run_controls

    return _measure_wired_stratum(
        STRATUM_COMMONS,
        controls_path,
        run_controls,
        "no external Commons retrieval/temporal control battery exists yet -- the 10 "
        "externally-sourced Commons controls referenced in spec A2.4.3 have no fixture file "
        "in this repository (only tests/fixtures/lords_retrieval_controls.json exists).",
        "scripts/run_commons_controls.py",
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
    never silently swallowed. `scripts/run_ec_controls.py` (REUSED here)
    closes the fixture gap with a 12-row control battery of real donations
    fetched live from the EC's own `/api/csv/Donations` export.
    """
    from scripts.run_ec_controls import run_controls

    return _measure_wired_stratum(
        STRATUM_ELECTORAL_COMMISSION,
        controls_path,
        run_controls,
        "no external Electoral Commission control battery exists yet, AND this stratum is "
        "not in run_gold_benchmark.MATERIAL_STRATA at all -- a passing gate here would "
        "currently have no effect on the scorer's verdict logic either way (see "
        "donation_edges_are_ungated_in_scorer).",
        "scripts/run_ec_controls.py",
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


def compute_control_fixtures_hash(
    lords_controls_path: str | Path | None = DEFAULT_LORDS_CONTROLS_PATH,
    ch_controls_path: str | Path | None = DEFAULT_CH_CONTROLS_PATH,
    commons_controls_path: str | Path | None = DEFAULT_COMMONS_CONTROLS_PATH,
    ec_controls_path: str | Path | None = DEFAULT_EC_CONTROLS_PATH,
) -> str:
    """sha256 over the CONTENT of every control-battery fixture this module
    was invoked with, in a fixed order -- so `GateFreezeState.control_fixtures
    _hash` (`uncorrupt.gates.binding`) can bind a stratum-gate artifact to
    exactly which fixture BYTES produced its numbers (module docstring's
    "the fixture is unbound" gap). Every path argument feeds the hash the
    same way `_measure_wired_stratum` reads it: missing (`None`) or a
    nonexistent file each hash to their own distinct marker, so a fixture
    silently going missing changes this hash exactly as much as a fixture
    silently being edited does -- neither can pass unnoticed as "no change".
    """
    h = hashlib.sha256()
    for path in (
        lords_controls_path,
        ch_controls_path,
        commons_controls_path,
        ec_controls_path,
    ):
        if path is None:
            h.update(b"<none>\n")
            continue
        p = Path(path)
        if not p.exists():
            h.update(f"<missing:{p}>\n".encode())
            continue
        h.update(p.read_bytes())
        h.update(b"\n")
    return h.hexdigest()


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
