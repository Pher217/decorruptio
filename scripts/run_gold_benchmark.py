"""Run the pre-registered Phase C gold-manifest benchmark -- ONE verdict.

Spec (LOCKED, do not deviate): vault
`02 Projects/Ideas/Decorruptio/05 Specs/phase-c-gold-manifest-preregistration.md`,
including AMENDMENT v2 (temporal evidence ladder), v2.1 (unit of analysis +
revised verdict set) and v2.3 (manifest schema semantics + the narrowed case
key). Sections 5-7 and all three amendments are binding.

This script does NOT implement a second path-search or resolution stack. It
calls the same code the rest of Phase C already uses and already trusts:

  * `scripts.phase_c_paths.find_paths`       -- the 2-hop path search
  * `scripts.phase_c_paths.resolve_supplier` -- company-side resolution
  * `scripts.phase_c_paths.resolve_referrer` -- person-side (surname) resolution
  * `scripts.phase_c_paths.build_adjacency`  -- the adjacency index
  * `scripts/run_positive_controls.py` / `scripts/run_negative_controls.py`
    -- invoked as subprocesses (never re-implemented) for the RETRIEVAL
    control numbers.

It also does NOT implement the spec A2.3 temporal control gate (the level-1/2
evidence classifier living in `src/uncorrupt/graph/register_snapshots.py` and
`scripts/measure_temporal_lift.py`, built separately). It only requires that
gate's *result* as an explicit input -- see `TemporalGate` / `load_temporal_gate`
-- so this runner can never emit REFUTED (or CONFIRMED) without one having
actually been measured.

UNIT OF ANALYSIS (amendment v2.1 A2.1.1, NARROWED by v2.3 A2.3.2): every
threshold below is scored at CASE level, not row level, and a case is the
distinct AWARDEE `company_number` alone -- not `(company_number, award_date)`
as v2.1 first set it. PPE Medpro's two separate DHSC awards (2020-06-12,
2020-06-25) arising from one underlying relationship must never score as two
recovered cases: what determines recovery is officer coverage of the
awardee company, so rows sharing an awardee are correlated, not independent.
`evaluate_case` rolls a case's constituent rows up into one status
(recovered beats undated_only beats not_recovered beats untestable beats
no_trace_by_design) using the case's EARLIEST qualifying award date as the
cutoff for every row -- the strictest cutoff available, since a relationship
pre-dating the earliest award necessarily pre-dates every later one too.
Row-level counts, and each case's row/award counts, are still computed and
reported, but never as the headline figure (A2.1.1: "Row-level alone is
forbidden as a headline").

`company_number` always means the AWARDEE (spec A2.3.1) -- the loader
(`scripts/load_gold_manifest.py`) rejects any row that does not explicitly
confirm this, so this script can assume it throughout.

Four per-row / per-case outcomes that MUST NOT be conflated -- Phase C v1
collapsed (c) into a refutation and had to retract "0 of 52" because of it:

  (a) recovered           -- a path was found and every edge on it dates
                             before the case's earliest award_date (H1's
                             actual claim).
  (b) undated_only        -- a path was found but at least one edge on it
                             has no valid_from, so pre-award is UNDECIDABLE,
                             not false.
  (c) untestable          -- the supplier or the referrer never resolved to
                             a graph entity. Excluded from every denominator;
                             reported separately with the specific reason.
  (d) no_trace_by_design  -- spec A2.3.3: the row's only evidence is
                             Persons-with-Significant-Control data
                             (`established_by: PSC`, `GoldRow.is_psc_sourced`).
                             PSC is a label source only, never ingested for
                             retrieval, so a `not_recovered` result on such a
                             row is an EXPECTED, honest "no trace" -- never a
                             refutation. Only a `not_recovered` PSC row is
                             relabelled this way; a PSC row that IS recovered
                             through independent register evidence is a
                             genuine recovery and is left alone.

Only a resolved, non-PSC-only pair with no path at all, dated or undated, is
a genuine `not_recovered` miss.

Source separation (spec SS3): a row's `excluded_from_retrieval` sources must
never be what makes its path recoverable. `check_source_separation` is a
BEST-EFFORT check against `Attestation.source_name` -- see its docstring for
exactly what it can and cannot prove.

VERDICT SET (amendment v2.1, A2.1.2 -- replaces the old SS6 table):

  INVALID            retrieval controls <9/10 -- pipeline broken, positives
                      result must not be reported.
  INSTRUMENT-LIMITED  retrieval controls pass but the temporal control gate
                      (A2.3) fails -- the UK strict hypothesis is untestable
                      with these sources.
  CONFIRMED           >=4/20 cases recovered, >=80% benchmark precision,
                      retrieval AND temporal gates both pass.
  REFUTED             0/20 cases recovered, retrieval AND temporal gates
                      both pass.
  PARTIAL             1-3/20 cases recovered, retrieval AND temporal gates
                      both pass. Real traces below the confirmation bar --
                      never rendered as a confirmation.

`classify_outcome` evaluates these in a strict priority order (INVALID, then
INSTRUMENT-LIMITED, then the case-count-dependent branches) so every one of
the five rows above maps to exactly one branch with no residual ambiguity --
unlike the SS6 table's REFUTED/COUNTRY SWITCH overlap this amendment fixes.

COUNTRY_SWITCH IS NOT A VERDICT (A2.1.2) -- it is an action triggered by
PARTIAL, REFUTED, or INSTRUMENT-LIMITED. See `country_switch_triggered`.

Statistical reporting (amendment A2.5): a `0/N` control or negative-control
count is never printed bare -- `wilson_upper_bound` computes the ~95%
one-sided upper bound reported alongside it. Benchmark precision (on the
constructed 20-ish-case : 200-negative sample) is reported labelled as such,
never as an operational/field precision figure, alongside sensitivity and
false-positive rate as separate, explicitly named metrics.

Usage:
    PYTHONPATH=.:src python scripts/run_gold_benchmark.py \\
        --manifest data/gold_manifest.csv \\
        --temporal-gate-report experiments/temporal_gate.json \\
        --out experiments/gold_benchmark.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from scripts.load_gold_manifest import GoldCase, GoldRow, load_gold_manifest  # noqa: E402
from scripts.phase_c_paths import (  # noqa: E402
    build_adjacency,
    find_paths,
    resolve_referrer,
    resolve_supplier,
    surname,
)

from uncorrupt.graph.models import Edge, Entity  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Spec SS6 / A2.1.2 LOCKED thresholds.
CONTROLS_PASS_FRACTION = 0.9  # >=9/10 retrieval controls
CONFIRM_MIN_CASES = 4  # >=4/20 cases (not rows -- A2.1.1)
CONFIRM_MIN_PRECISION = 0.80  # >=80% benchmark precision

# Priority order for rolling per-row statuses up to a case (higher wins).
# no_trace_by_design (spec A2.3.3) ranks lowest: it is an EXPECTED non-result
# on a PSC-only row, so any other row in the case -- even a mere untestable
# resolution gap -- is more informative and wins the roll-up.
_STATUS_PRIORITY = {
    "recovered": 4,
    "undated_only": 3,
    "not_recovered": 2,
    "untestable": 1,
    "no_trace_by_design": 0,
}

_Z_95 = 1.959963984540054  # two-sided 95% normal quantile


@dataclass
class RowEvaluation:
    case_id: str
    status: str  # recovered | undated_only | not_recovered | untestable | no_trace_by_design
    reason: str | None = None
    source_separation: str = "not_applicable"
    example_path: list[str] = field(default_factory=list)


@dataclass
class CaseEvaluation:
    """The case-level roll-up of one `GoldCase`'s constituent rows (A2.1.1, A2.3.2)."""

    case_key: str
    company_number: str
    row_count: int
    award_count: int
    earliest_award_date: str
    row_case_ids: list[str]
    status: str  # recovered | undated_only | not_recovered | untestable | no_trace_by_design
    source_separation: str
    row_evaluations: list[RowEvaluation]

    @property
    def is_concentrated(self) -> bool:
        return self.row_count > 1


@dataclass(frozen=True)
class TemporalGate:
    """Spec A2.3 temporal control gate -- a REQUIRED input to `classify_outcome`.

    Built by a separate classifier out of this script's scope
    (`src/uncorrupt/graph/register_snapshots.py`,
    `scripts/measure_temporal_lift.py`). This script never computes it; it
    only requires the caller supply the result, so REFUTED (and CONFIRMED)
    can never be emitted without one having actually been measured.

    `passed` reflects spec A2.3 in full: ~27/30 controls recovered at
    evidence level 1 (event-dated) or level 2 (pre-award observed) overall,
    AND >=9/10 within each material relationship-type/source stratum
    (`failing_strata` names any stratum that did not clear that bar).
    """

    passed: bool
    overall_recovered: int | None = None
    overall_total: int | None = None
    failing_strata: tuple[str, ...] = ()


def load_temporal_gate(path: Path) -> TemporalGate | None:
    """Load the spec A2.3 temporal control gate result, if it has been measured.

    Expected JSON contract, produced by the separate temporal-lift classifier
    (not implemented here):

        {
          "passed": bool,
          "overall_recovered": int,
          "overall_total": int,
          "failing_strata": ["<relationship_type/source>", ...]
        }

    Returns None if the file does not exist. `classify_outcome` treats `None`
    exactly like `passed=False` -- a missing measurement is never an implicit
    pass (spec A2.3: "REFUTED may be reported only when ... the temporal
    control gate passes").
    """
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    return TemporalGate(
        passed=bool(data["passed"]),
        overall_recovered=data.get("overall_recovered"),
        overall_total=data.get("overall_total"),
        failing_strata=tuple(data.get("failing_strata", ())),
    )


def _resolve_referrer_entities(
    row: GoldRow, people_by_surname: dict[str, list[Entity]]
) -> list[Entity]:
    """Registry ID first when the manifest supplies one, surname second.

    Mirrors `resolve_supplier`'s own priority order (a registry identifier
    beats name-derived matching) rather than inventing a different rule for
    the person side of the row.
    """
    if row.person_registry_id:
        by_id = list(
            Entity.objects.filter(entity_type="person", registry_id=row.person_registry_id)
        )
        if by_id:
            return by_id
    return resolve_referrer(row.person_name, people_by_surname)


def _path_taint(path: list[Edge], lowered_excluded: list[str]) -> str:
    """Classify one path as 'clean', 'tainted', or 'unverifiable'.

    'tainted' fires the moment any edge on the path carries an attestation
    whose source_name matches an excluded string (case-insensitive substring,
    either direction). 'unverifiable' means no tainted match was found, but
    at least one edge on the path has zero attestations -- its provenance is
    simply not recorded, so the absence of a match is not proof of
    separation.
    """
    saw_unattested = False
    for edge in path:
        atts = list(edge.attestations.all())
        if not atts:
            saw_unattested = True
            continue
        for att in atts:
            sn = (att.source_name or "").lower()
            if any(exc in sn or sn in exc for exc in lowered_excluded):
                return "tainted"
    return "unverifiable" if saw_unattested else "clean"


def check_source_separation(paths: list[list[Edge]], excluded_sources: tuple[str, ...]) -> str:
    """Best-effort spec SS3 check: did an excluded source make this recoverable?

    Judged 'ok' if AT LEAST ONE found path is 'clean' (no excluded-source
    attestation, no unattested gap) -- an independent, permitted path exists.
    If none are clean but at least one is 'unverifiable', the result is
    'cannot_verify'. Only when every path is positively 'tainted' is the
    result 'violation'.

    KNOWN LIMITS (reported, not hidden):
      * Can only see what was recorded as an `Attestation.source_name`. It
        cannot detect an excluded source that shaped entity resolution or
        ingestion without ever being logged as an attestation.
      * Matching is a free-text, case-insensitive substring test -- only as
        good as how closely `excluded_from_retrieval` wording matches this
        project's own `source_name` conventions.
    """
    if not excluded_sources or not paths:
        return "not_applicable"
    lowered = [s.lower() for s in excluded_sources if s.strip()]
    if not lowered:
        return "not_applicable"
    taints = [_path_taint(p, lowered) for p in paths]
    if "clean" in taints:
        return "ok"
    if "unverifiable" in taints:
        return "cannot_verify"
    return "violation"


def _resolve_pair(
    row: GoldRow,
    adj: dict[int, list[Edge]],
    people_by_surname: dict[str, list[Entity]],
    ch_cache: dict,
    max_hops: int,
    cutoff: date,
) -> RowEvaluation:
    """Core recovered/undated_only/not_recovered/untestable decision for one
    row, at an explicit `cutoff`.

    Shared by `evaluate_row` (the row's own `award_date`) and `evaluate_case`
    (the case's `earliest_award_date`, spec A2.3.2) so the resolve + path
    search + source-separation logic exists in exactly one place.
    """
    supplier = resolve_supplier(row.company_name, ch_cache, row.company_number)
    referrers = _resolve_referrer_entities(row, people_by_surname)

    if not supplier or not referrers:
        missing = []
        if not supplier:
            missing.append("supplier")
        if not referrers:
            missing.append("referrer")
        return RowEvaluation(
            case_id=row.case_id,
            status="untestable",
            reason=f"unresolved: {', '.join(missing)}",
        )

    pre_award, undated = find_paths(
        {r.id for r in referrers}, supplier.id, adj, max_hops, cutoff=cutoff
    )

    if pre_award:
        sep = check_source_separation(pre_award, row.excluded_from_retrieval)
        return RowEvaluation(
            case_id=row.case_id,
            status="recovered",
            source_separation=sep,
            example_path=[f"{e.edge_type}@{e.valid_from}" for e in pre_award[0]],
        )
    if undated:
        sep = check_source_separation(undated, row.excluded_from_retrieval)
        return RowEvaluation(
            case_id=row.case_id,
            status="undated_only",
            reason="path found but temporally undecidable (spec SS7.2)",
            source_separation=sep,
            example_path=[f"{e.edge_type}@{e.valid_from}" for e in undated[0]],
        )
    return RowEvaluation(case_id=row.case_id, status="not_recovered")


def _apply_psc_relabel(row: GoldRow, evaluation: RowEvaluation) -> RowEvaluation:
    """Spec A2.3.3: a PSC-sourced row that finds no path is an EXPECTED
    "no trace" -- PSC is a label source only, never ingested for retrieval --
    never a refutation.

    Relabels a `not_recovered` PSC row to `no_trace_by_design`. Every other
    status is left untouched: a PSC row that IS recovered through
    independent register evidence is a genuine recovery, not overridden, and
    an `untestable` PSC row is still a real resolution gap worth its own
    label, not conflated with the PSC caveat.
    """
    if row.is_psc_sourced and evaluation.status == "not_recovered":
        return RowEvaluation(
            case_id=evaluation.case_id,
            status="no_trace_by_design",
            reason=(
                "PSC is a label source only, not ingested for retrieval -- expected "
                "no-trace, not a refutation (spec A2.3.3)"
            ),
            source_separation=evaluation.source_separation,
            example_path=evaluation.example_path,
        )
    return evaluation


def evaluate_row(
    row: GoldRow,
    adj: dict[int, list[Edge]],
    people_by_surname: dict[str, list[Entity]],
    ch_cache: dict,
    max_hops: int,
) -> RowEvaluation:
    """Classify one gold row on its OWN `award_date` as the cutoff.

    Reuses `resolve_supplier` and `find_paths` exactly as Phase C's other
    scripts do, via the shared `_resolve_pair` helper. Note that the BINDING
    case-level test (`evaluate_case`) uses the case's earliest qualifying
    award date, not each row's own -- this function is the standalone,
    per-row view (spec SS2.3's per-row admissibility question), not the
    scored recovery test.
    """
    result = _resolve_pair(row, adj, people_by_surname, ch_cache, max_hops, cutoff=row.award_date)
    return _apply_psc_relabel(row, result)


def evaluate_case(
    case: GoldCase,
    adj: dict[int, list[Edge]],
    people_by_surname: dict[str, list[Entity]],
    ch_cache: dict,
    max_hops: int,
) -> CaseEvaluation:
    """Roll up every row in a case into ONE case-level status (spec A2.1.1, A2.3.2).

    Every row is tested against the CASE's earliest qualifying award date
    (`case.earliest_award_date`), not its own -- the strictest cutoff
    available, since a relationship pre-dating the earliest award
    necessarily pre-dates every later award from the same awardee too.

    A case's status is the STRONGEST status achieved by any of its rows
    (recovered > undated_only > not_recovered > untestable >
    no_trace_by_design): a case counts as recovered if any one of the people
    tied to it produces a pre-award path, even if others don't resolve at
    all. This is exactly "additional people on the same case may raise
    confidence within that case; they never multiply it" -- five recovered
    rows on one case still contribute exactly ONE recovered case, never
    five; likewise multiple awards to the same company are one case, not one
    per award.
    """
    cutoff = case.earliest_award_date
    row_evals = [
        _apply_psc_relabel(
            row, _resolve_pair(row, adj, people_by_surname, ch_cache, max_hops, cutoff)
        )
        for row in case.rows
    ]
    status = max(row_evals, key=lambda r: _STATUS_PRIORITY[r.status]).status

    if status in ("recovered", "undated_only"):
        contributing = [r for r in row_evals if r.status == status]
        seps = [r.source_separation for r in contributing]
        if "ok" in seps:
            sep = "ok"
        elif "cannot_verify" in seps:
            sep = "cannot_verify"
        elif "violation" in seps:
            sep = "violation"
        else:
            sep = "not_applicable"
    else:
        sep = "not_applicable"

    return CaseEvaluation(
        case_key=case.case_key,
        company_number=case.company_number,
        row_count=case.row_count,
        award_count=case.award_count,
        earliest_award_date=case.earliest_award_date.isoformat(),
        row_case_ids=[r.case_id for r in case.rows],
        status=status,
        source_separation=sep,
        row_evaluations=row_evals,
    )


def wilson_upper_bound(successes: int, n: int, z: float = _Z_95) -> float:
    """Wilson score interval upper bound for a binomial proportion.

    Spec A2.5: "0/200 is not a zero false-positive rate" -- its approximate
    95% upper bound must always be reported alongside a raw count, never a
    bare zero. This uses the standard Wilson interval (works for any x, not
    just x=0) rather than the simpler rule-of-three heuristic (`3/n`) the
    amendment text illustrates its point with; at x=0, n=200 this gives
    ~1.9% versus the amendment's quoted ~1.5% -- both are approximations of
    the same 95% bound, and this is the more general, standard one.
    """
    if n == 0:
        return 0.0
    phat = successes / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    adjustment = z * ((phat * (1 - phat) / n + z * z / (4 * n * n)) ** 0.5)
    return (centre + adjustment) / denom


def compute_precision(cases_recovered: int, negatives_recovered: int) -> float:
    """BENCHMARK precision over the constructed case-control sample (spec
    SS5/A2.5): `cases_recovered` (case-level, per A2.1.1/A2.3.2) against a
    spurious hit count from the 200 matched negatives.

    Spec A2.5 is explicit that this is benchmark precision on a constructed
    ~20:200 sample, NOT expected field precision at real-world prevalence --
    callers must label it as such and report sensitivity / false-positive
    rate as separate figures, never substitute this number for either.
    """
    denom = cases_recovered + negatives_recovered
    return cases_recovered / denom if denom > 0 else 0.0


def classify_outcome(
    cases_recovered: int,
    precision: float,
    retrieval_controls_recovered: int,
    retrieval_controls_total: int,
    temporal_gate: TemporalGate | None,
) -> str:
    """Spec A2.1.2 (amendment v2.1) LOCKED verdict set, all case-level.

    Strict priority order -- each of the five verdicts maps to exactly one
    branch, with no overlap (unlike the original SS6 table's REFUTED/COUNTRY
    SWITCH ambiguity this amendment replaces):

      1. retrieval controls fail (<9/10)      -> INVALID
      2. temporal gate fails (A2.3)            -> INSTRUMENT-LIMITED
      3. (both gates pass) >=4 cases & >=80%   -> CONFIRMED
      4. (both gates pass) 0 cases             -> REFUTED
      5. (both gates pass) 1-3 cases           -> PARTIAL

    `temporal_gate` is REQUIRED and may be `None` (not yet measured);
    `None` is treated identically to a failing gate -- it can NEVER route to
    CONFIRMED or REFUTED. This is the mechanism spec A2.3 demands: "REFUTED
    may be reported only when ... the temporal control gate passes."
    """
    retrieval_pass = (
        retrieval_controls_total > 0
        and (retrieval_controls_recovered / retrieval_controls_total) >= CONTROLS_PASS_FRACTION
    )
    if not retrieval_pass:
        return "INVALID"

    temporal_pass = temporal_gate is not None and temporal_gate.passed
    if not temporal_pass:
        return "INSTRUMENT-LIMITED"

    if cases_recovered >= CONFIRM_MIN_CASES and precision >= CONFIRM_MIN_PRECISION:
        return "CONFIRMED"
    if cases_recovered == 0:
        return "REFUTED"
    return "PARTIAL"


def country_switch_triggered(outcome: str) -> bool:
    """Spec A2.1.2: COUNTRY_SWITCH is an ACTION, not a verdict.

    Triggered by PARTIAL, REFUTED, or INSTRUMENT-LIMITED -- i.e. by every
    verdict except CONFIRMED and INVALID (a broken pipeline licenses no
    action at all until it is fixed).
    """
    return outcome in ("PARTIAL", "REFUTED", "INSTRUMENT-LIMITED")


def _run_control_script(script_name: str, extra_args: list[str]) -> dict:
    out_path = REPO_ROOT / "experiments" / f"_gold_benchmark_{script_name}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / script_name),
        "--out",
        str(out_path),
        *extra_args,
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = f".:{REPO_ROOT / 'src'}"
    subprocess.run(cmd, check=True, cwd=REPO_ROOT, env=env)
    return json.loads(out_path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="data/gold_manifest.csv")
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--controls-n", type=int, default=10)
    parser.add_argument("--negatives-n", type=int, default=200)
    parser.add_argument(
        "--temporal-gate-report",
        default="experiments/temporal_gate.json",
        help=(
            "path to the spec A2.3 temporal control gate result, produced by the "
            "separate temporal-lift classifier (scripts/measure_temporal_lift.py). "
            "If absent, the gate is treated as failing -- see load_temporal_gate()."
        ),
    )
    parser.add_argument("--out", default="experiments/gold_benchmark.json")
    parser.add_argument(
        "--skip-controls",
        action="store_true",
        help=(
            "reuse experiments/positive_controls.json and negative_controls.json instead of "
            "re-running them. NOT recommended: spec SS7.1 says the negative rate expires after "
            "every ingest."
        ),
    )
    args = parser.parse_args()

    manifest_result = load_gold_manifest(args.manifest)
    concentrated_cases = [c for c in manifest_result.cases if c.is_concentrated]
    psc_rows = [r for r in manifest_result.admissible if r.is_psc_sourced]

    print(f"=== GOLD MANIFEST: {args.manifest} ===")
    print(f"admissible rows : {len(manifest_result.admissible)}")
    print(
        f"distinct cases  : {len(manifest_result.cases)}"
        "  (unit of analysis -- awardee company, spec A2.3.2)"
    )
    print(f"inadmissible    : {len(manifest_result.inadmissible)}")
    for row in manifest_result.inadmissible:
        print(f"  REJECTED {row.case_id}: {'; '.join(row.reasons)}")
    if concentrated_cases:
        print(
            "\nconcentrated cases (>1 row -- spec A2.1.1/A2.3.2 requires these listed explicitly):"
        )
        for case in concentrated_cases:
            print(
                f"  {case.case_key}: {case.row_count} rows, {case.award_count} distinct award(s) "
                f"-- {[r.case_id for r in case.rows]}"
            )
    if psc_rows:
        print(
            f"\nPSC-sourced rows (spec A2.3.3, expected unrecoverable by design): "
            f"{len(psc_rows)} -- {[r.case_id for r in psc_rows]}"
        )

    if args.skip_controls:
        positive_controls = json.loads(
            (REPO_ROOT / "experiments" / "positive_controls.json").read_text()
        )
        negative_controls = json.loads(
            (REPO_ROOT / "experiments" / "negative_controls.json").read_text()
        )
    else:
        print("\nrunning positive (retrieval) controls (fresh)...")
        positive_controls = _run_control_script(
            "run_positive_controls.py", ["--n", str(args.controls_n)]
        )
        print("running negative controls (fresh -- spec SS7.1: this rate expires per ingest)...")
        negative_controls = _run_control_script(
            "run_negative_controls.py", ["--n", str(args.negatives_n)]
        )

    retrieval_controls_recovered = positive_controls["retrieved"]
    retrieval_controls_total = positive_controls["n"]
    if retrieval_controls_total != 10:
        print(
            f"NOTE: retrieval controls_total={retrieval_controls_total}, not the spec SS5/SS6 "
            f"cohort size of 10 -- the >=9/10 threshold is applied proportionally.",
            file=sys.stderr,
        )
    negatives_recovered = negative_controls["with_path"]
    negatives_total = negative_controls["n"]

    temporal_gate = load_temporal_gate(Path(args.temporal_gate_report))
    if temporal_gate is None:
        print(
            f"\nWARNING: no temporal control gate report at {args.temporal_gate_report} -- "
            "spec A2.3 requires one before REFUTED or CONFIRMED can be reported. This run's "
            "outcome is capped at INSTRUMENT-LIMITED (or INVALID) until it exists.",
            file=sys.stderr,
        )

    people_by_surname: dict[str, list[Entity]] = {}
    for person in Entity.objects.filter(entity_type="person"):
        sn = surname(person.name)
        if sn:
            people_by_surname.setdefault(sn, []).append(person)

    adj = build_adjacency()
    print(f"\ngraph: {Entity.objects.count()} entities, {Edge.objects.count()} edges")

    case_evaluations = [
        evaluate_case(case, adj, people_by_surname, {}, args.max_hops)
        for case in manifest_result.cases
    ]
    row_evaluations = [re for ce in case_evaluations for re in ce.row_evaluations]

    def _count(evals: list, status: str) -> int:
        return sum(1 for e in evals if e.status == status)

    cases_recovered = _count(case_evaluations, "recovered")
    cases_undated_only = _count(case_evaluations, "undated_only")
    cases_not_recovered = _count(case_evaluations, "not_recovered")
    cases_untestable = _count(case_evaluations, "untestable")
    cases_no_trace_by_design = _count(case_evaluations, "no_trace_by_design")

    rows_recovered = _count(row_evaluations, "recovered")
    rows_undated_only = _count(row_evaluations, "undated_only")
    rows_not_recovered = _count(row_evaluations, "not_recovered")
    rows_untestable = _count(row_evaluations, "untestable")
    rows_no_trace_by_design = _count(row_evaluations, "no_trace_by_design")

    benchmark_precision = compute_precision(cases_recovered, negatives_recovered)
    sensitivity = cases_recovered / len(manifest_result.cases) if manifest_result.cases else 0.0
    false_positive_rate = negatives_recovered / negatives_total if negatives_total else 0.0
    negatives_fp_upper_95 = wilson_upper_bound(negatives_recovered, negatives_total)

    outcome = classify_outcome(
        cases_recovered,
        benchmark_precision,
        retrieval_controls_recovered,
        retrieval_controls_total,
        temporal_gate,
    )
    switch_action = country_switch_triggered(outcome)

    violations = [c for c in case_evaluations if c.source_separation == "violation"]
    unverifiable = [c for c in case_evaluations if c.source_separation == "cannot_verify"]

    print(f"\n=== PHASE C GOLD BENCHMARK (max {args.max_hops} hops, case-level) ===")
    print(f"cases total (distinct awardees)    : {len(manifest_result.cases)}")
    print(f"  recovered (pre-award)            : {cases_recovered}")
    print(
        f"  undated_only (temporal unknown)  : {cases_undated_only}"
        "  -- never recovery, never refutation"
    )
    print(f"  not_recovered (real miss)        : {cases_not_recovered}")
    print(f"  untestable (unresolved)          : {cases_untestable}  -- excluded from denominators")
    print(
        f"  no_trace_by_design (PSC, A2.3.3) : {cases_no_trace_by_design}"
        "  -- expected, never a refutation"
    )
    print(
        f"rows (secondary, non-headline)    : recovered={rows_recovered} "
        f"undated_only={rows_undated_only} not_recovered={rows_not_recovered} "
        f"untestable={rows_untestable} no_trace_by_design={rows_no_trace_by_design}"
    )
    print(
        f"retrieval controls                : {retrieval_controls_recovered}/"
        f"{retrieval_controls_total}"
    )
    print(
        "temporal control gate              : "
        f"{'PASS' if temporal_gate and temporal_gate.passed else 'FAIL/UNMEASURED'}"
        + (
            f" ({temporal_gate.overall_recovered}/{temporal_gate.overall_total}, "
            f"failing strata: {list(temporal_gate.failing_strata)})"
            if temporal_gate is not None
            else ""
        )
    )
    print(
        f"negative spurious rate (any path)  : {negatives_recovered}/{negatives_total} "
        f"(95% CI upper bound: {negatives_fp_upper_95:.1%}) -- spec A2.5: never a bare zero"
    )
    print(f"benchmark precision (NOT field precision, spec A2.5): {benchmark_precision:.3f}")
    print(f"sensitivity (recall)               : {sensitivity:.3f}")
    print(f"false_positive_rate                : {false_positive_rate:.3f}")
    print(f"\n>>> OUTCOME: {outcome} <<<")
    print(f">>> COUNTRY_SWITCH action triggered: {switch_action} <<<")
    if violations:
        print(
            f"\nWARNING: spec SS3 source-separation VIOLATION on "
            f"{len(violations)} case(s): {[c.case_key for c in violations]}"
        )
    if unverifiable:
        print(
            f"\nNOTE: source separation could NOT be verified for {len(unverifiable)} case(s) "
            f"(unattested edge on every found path): {[c.case_key for c in unverifiable]}"
        )

    report = {
        "outcome": outcome,
        "country_switch_triggered": switch_action,
        "benchmark_precision": benchmark_precision,
        "sensitivity": sensitivity,
        "false_positive_rate": false_positive_rate,
        "cases": {
            "total": len(manifest_result.cases),
            "recovered": cases_recovered,
            "undated_only": cases_undated_only,
            "not_recovered": cases_not_recovered,
            "untestable": cases_untestable,
            "no_trace_by_design": cases_no_trace_by_design,
            "concentrated": [
                {
                    "case_key": c.case_key,
                    "row_count": c.row_count,
                    "award_count": c.award_count,
                    "row_case_ids": c.row_case_ids,
                }
                for c in concentrated_cases
            ],
        },
        "rows": {
            "total": len(manifest_result.admissible),
            "recovered": rows_recovered,
            "undated_only": rows_undated_only,
            "not_recovered": rows_not_recovered,
            "untestable": rows_untestable,
            "no_trace_by_design": rows_no_trace_by_design,
            "psc_sourced": [r.case_id for r in psc_rows],
            "note": "secondary figure only -- spec A2.1.1 forbids row-level as a headline",
        },
        "retrieval_controls": {
            "recovered": retrieval_controls_recovered,
            "total": retrieval_controls_total,
        },
        "temporal_gate": {
            "measured": temporal_gate is not None,
            "passed": bool(temporal_gate and temporal_gate.passed),
            "overall_recovered": temporal_gate.overall_recovered if temporal_gate else None,
            "overall_total": temporal_gate.overall_total if temporal_gate else None,
            "failing_strata": list(temporal_gate.failing_strata) if temporal_gate else [],
        },
        "negative_controls": {
            "any_path": negatives_recovered,
            "n": negatives_total,
            "upper_bound_95pct": negatives_fp_upper_95,
        },
        "source_separation": {
            "violations": [c.case_key for c in violations],
            "unverifiable": [c.case_key for c in unverifiable],
            "known_limits": (
                "Best-effort Attestation.source_name substring match only. Cannot detect an "
                "excluded source that shaped resolution/ingestion without being logged as an "
                "attestation, nor a wording mismatch between excluded_from_retrieval text and "
                "source_name conventions."
            ),
        },
        "inadmissible_rows": [
            {"case_id": r.case_id, "reasons": list(r.reasons)} for r in manifest_result.inadmissible
        ],
        "case_detail": [
            {
                "case_key": c.case_key,
                "company_number": c.company_number,
                "row_count": c.row_count,
                "award_count": c.award_count,
                "earliest_award_date": c.earliest_award_date,
                "row_case_ids": c.row_case_ids,
                "status": c.status,
                "source_separation": c.source_separation,
            }
            for c in case_evaluations
        ],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
