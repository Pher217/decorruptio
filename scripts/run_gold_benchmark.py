"""Run the pre-registered Phase C gold-manifest benchmark -- ONE verdict.

Spec (LOCKED, do not deviate): vault
`02 Projects/Ideas/Decorruptio/05 Specs/phase-c-gold-manifest-preregistration.md`.
Sections 5 (cohort composition), 6 (acceptance thresholds) and 7 (standing
caveats) are binding here.

This script does NOT implement a second path-search or resolution stack. It
calls the same code the rest of Phase C already uses and already trusts:

  * `scripts.phase_c_paths.find_paths`       -- the 2-hop path search
  * `scripts.phase_c_paths.resolve_supplier` -- company-side resolution
  * `scripts.phase_c_paths.resolve_referrer` -- person-side (surname) resolution
  * `scripts.phase_c_paths.build_adjacency`  -- the adjacency index
  * `scripts/run_positive_controls.py` / `scripts/run_negative_controls.py`
    -- invoked as subprocesses (never re-implemented) so the control numbers
    quoted are exactly the ones the project already relies on.

Three outcomes for a single manifest row that MUST NOT be conflated -- Phase
C v1 collapsed (c) into a refutation and had to retract "0 of 52" because of
it:

  (a) recovered      -- a path was found and every edge on it dates before
                        the row's own award_date (H1's actual claim).
  (b) undated_only    -- a path was found but at least one edge on it has no
                        valid_from, so pre-award is UNDECIDABLE for this row,
                        not false. Standing caveat SS7.2: only ~0.4% of
                        `declared_interest` edges carry a date at all (the
                        Lords register publishes no start dates), against
                        92.3% of `officer_of`. A row whose only graph
                        evidence is a declared-interest edge lands here BY
                        CONSTRUCTION regardless of whether the relationship
                        is real. Never counted as a recovery, never as a
                        refutation.
  (c) untestable      -- the supplier or the referrer never resolved to a
                        graph entity. Excluded from every denominator below;
                        reported separately with the specific reason.

Only a resolved pair with no path at all, dated or undated, is a genuine
`not_recovered` miss -- the only category that is real evidence against H1
for that row.

Source separation (spec SS3): a row's `excluded_from_retrieval` sources must
never be what makes its path recoverable. `check_source_separation` below is
a BEST-EFFORT check against `Attestation.source_name` on the recovered
path(s) -- see its docstring for exactly what it can and cannot prove. It is
not a guarantee, and this script's output says so explicitly rather than
implying enforcement it cannot deliver.

Two places this script makes an explicit judgement call where the spec text
underspecifies the mechanism -- both are logged loudly in the printed report,
not buried:

  * REFUTED vs COUNTRY_SWITCH (spec SS6 table): both rows key off "controls
    pass AND positives fail". REFUTED is treated as the strict 0/20 case
    (the one with a stated statistical bound); COUNTRY_SWITCH is the general
    "did not confirm" case for any other shortfall (1-3/20, or precision
    below 80%). INVALID is checked first, per SS6's "must not be reported".
  * Precision's false-positive term uses the negative controls' `with_path`
    (ANY 2-hop connection, dated or not) rather than `with_preaward`. The
    negative-control script's own pre-award figure is pinned to a fixed,
    VIP-lane-specific cutoff (2020-03-01) that has no relationship to the
    gold manifest's per-row award dates, so it is not a fair like-for-like
    false-positive baseline here. `with_path` is cutoff-agnostic and, if
    anything, an over-count of noise -- a conservative (lower) bound on true
    precision, which is the safer direction to err on for a pre-registered
    confirmatory test.

Usage:
    PYTHONPATH=.:src python scripts/run_gold_benchmark.py \\
        --manifest data/gold_manifest.csv \\
        --out experiments/gold_benchmark.json
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from scripts.load_gold_manifest import GoldRow, load_gold_manifest  # noqa: E402
from scripts.phase_c_paths import (  # noqa: E402
    build_adjacency,
    find_paths,
    resolve_referrer,
    resolve_supplier,
    surname,
)

from uncorrupt.graph.models import Edge, Entity  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Spec SS6 LOCKED thresholds.
CONTROLS_PASS_FRACTION = 0.9  # >=9/10
CONFIRM_MIN_RECOVERED = 4  # >=4/20
CONFIRM_MIN_PRECISION = 0.80  # >=80%


@dataclass
class RowEvaluation:
    case_id: str
    status: str  # "recovered" | "undated_only" | "not_recovered" | "untestable"
    reason: str | None = None
    source_separation: str = "not_applicable"
    example_path: list[str] = field(default_factory=list)


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
    """Best-effort spec SS3 check: did an excluded source make this row recoverable?

    A row's recovery is judged 'ok' if AT LEAST ONE of its found paths is
    'clean' (no excluded-source attestation, no unattested gap) -- an
    independent, permitted path exists. If none are clean but at least one
    is 'unverifiable', the result is 'cannot_verify'. Only when every path
    is positively 'tainted' is the result 'violation'.

    KNOWN LIMITS (reported, not hidden -- see the module docstring's
    "PRECISION" note for the sibling caveat on the negative-control cutoff):
      * This can only see what was recorded as an `Attestation.source_name`.
        It cannot detect an excluded source that shaped entity resolution or
        ingestion without ever being logged as an attestation.
      * Matching is a free-text, case-insensitive substring test. It is only
        as good as how closely a manifest's `excluded_from_retrieval`
        wording matches this project's own `source_name` conventions
        ("Companies House", "Electoral Commission", ...). A worded-
        differently exclusion will silently read as 'ok' or 'cannot_verify'
        rather than 'violation'.
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


def evaluate_row(
    row: GoldRow,
    adj: dict[int, list[Edge]],
    people_by_surname: dict[str, list[Entity]],
    ch_cache: dict,
    max_hops: int,
) -> RowEvaluation:
    """Classify one gold row into recovered / undated_only / not_recovered / untestable.

    Reuses `resolve_supplier` and `find_paths` exactly as Phase C's other
    scripts do. Critically, `find_paths` is called with `cutoff=row.award_date`
    -- each row's OWN award date, not the VIP-lane cohort's single fixed
    cutoff `find_paths` defaults to. That per-row cutoff is the entire reason
    the gold manifest schema carries an `award_date` column (spec SS2.3).
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
        {r.id for r in referrers}, supplier.id, adj, max_hops, cutoff=row.award_date
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


def compute_precision(positives_recovered: int, negatives_recovered: int) -> float:
    """Precision over the pooled labelled evaluation set (spec SS5: 20
    positives + 200 matched negatives). `negatives_recovered` should be the
    negative controls' ANY-path spurious count -- see the module docstring
    for why `with_preaward` is not used here.
    """
    denom = positives_recovered + negatives_recovered
    return positives_recovered / denom if denom > 0 else 0.0


def classify_outcome(
    positives_recovered: int,
    precision: float,
    controls_recovered: int,
    controls_total: int,
) -> str:
    """Spec SS6 LOCKED acceptance thresholds.

    INVALID is checked first: SS6 says explicitly the positives result "must
    not be reported" when controls fail, so nothing about positives may
    influence the outcome once that gate fires. See the module docstring for
    the REFUTED vs COUNTRY_SWITCH reading adopted here.
    """
    controls_pass = (
        controls_total > 0 and (controls_recovered / controls_total) >= CONTROLS_PASS_FRACTION
    )
    if not controls_pass:
        return "INVALID"
    if positives_recovered >= CONFIRM_MIN_RECOVERED and precision >= CONFIRM_MIN_PRECISION:
        return "CONFIRMED"
    if positives_recovered == 0:
        return "REFUTED"
    return "COUNTRY_SWITCH"


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
    print(f"=== GOLD MANIFEST: {args.manifest} ===")
    print(f"admissible  : {len(manifest_result.admissible)}")
    print(f"inadmissible: {len(manifest_result.inadmissible)}")
    for row in manifest_result.inadmissible:
        print(f"  REJECTED {row.case_id}: {'; '.join(row.reasons)}")

    if args.skip_controls:
        positive_controls = json.loads(
            (REPO_ROOT / "experiments" / "positive_controls.json").read_text()
        )
        negative_controls = json.loads(
            (REPO_ROOT / "experiments" / "negative_controls.json").read_text()
        )
    else:
        print("\nrunning positive controls (fresh)...")
        positive_controls = _run_control_script(
            "run_positive_controls.py", ["--n", str(args.controls_n)]
        )
        print("running negative controls (fresh -- spec SS7.1: this rate expires per ingest)...")
        negative_controls = _run_control_script(
            "run_negative_controls.py", ["--n", str(args.negatives_n)]
        )

    controls_recovered = positive_controls["retrieved"]
    controls_total = positive_controls["n"]
    if controls_total != 10:
        print(
            f"NOTE: controls_total={controls_total}, not the spec SS5/SS6 cohort size of 10 -- "
            f"the >=9/10 threshold is applied proportionally.",
            file=sys.stderr,
        )
    negatives_recovered = negative_controls["with_path"]

    people_by_surname: dict[str, list[Entity]] = {}
    for person in Entity.objects.filter(entity_type="person"):
        sn = surname(person.name)
        if sn:
            people_by_surname.setdefault(sn, []).append(person)

    adj = build_adjacency()
    print(f"\ngraph: {Entity.objects.count()} entities, {Edge.objects.count()} edges")

    evaluations = [
        evaluate_row(row, adj, people_by_surname, {}, args.max_hops)
        for row in manifest_result.admissible
    ]

    recovered = [e for e in evaluations if e.status == "recovered"]
    undated_only = [e for e in evaluations if e.status == "undated_only"]
    not_recovered = [e for e in evaluations if e.status == "not_recovered"]
    untestable = [e for e in evaluations if e.status == "untestable"]

    positives_recovered = len(recovered)
    precision = compute_precision(positives_recovered, negatives_recovered)
    outcome = classify_outcome(positives_recovered, precision, controls_recovered, controls_total)

    violations = [e for e in evaluations if e.source_separation == "violation"]
    unverifiable = [e for e in evaluations if e.source_separation == "cannot_verify"]

    print(f"\n=== PHASE C GOLD BENCHMARK (max {args.max_hops} hops) ===")
    print(f"positives total (admissible)     : {len(manifest_result.admissible)}")
    print(f"  recovered (pre-award)           : {positives_recovered}")
    print(
        f"  undated_only (temporal unknown) : {len(undated_only)}"
        "  -- never recovery, never refutation"
    )
    print(f"  not_recovered (real miss)       : {len(not_recovered)}")
    print(
        f"  untestable (unresolved)         : {len(untestable)}  -- excluded from every denominator"
    )
    print(f"controls (register-visible)      : {controls_recovered}/{controls_total}")
    print(f"negative spurious rate (any path): {negatives_recovered}/{negative_controls['n']}")
    print(f"precision                        : {precision:.3f}")
    print(f"\n>>> OUTCOME: {outcome} <<<")
    if violations:
        print(
            f"\nWARNING: spec SS3 source-separation VIOLATION on "
            f"{len(violations)} row(s): {[e.case_id for e in violations]}"
        )
    if unverifiable:
        print(
            f"\nNOTE: source separation could NOT be verified for {len(unverifiable)} row(s) "
            f"(unattested edge on every found path): {[e.case_id for e in unverifiable]}"
        )

    report = {
        "outcome": outcome,
        "precision": precision,
        "positives": {
            "admissible_total": len(manifest_result.admissible),
            "recovered": positives_recovered,
            "undated_only": len(undated_only),
            "not_recovered": len(not_recovered),
            "untestable": len(untestable),
        },
        "controls": {"recovered": controls_recovered, "total": controls_total},
        "negative_controls": {
            "any_path": negatives_recovered,
            "n": negative_controls["n"],
        },
        "source_separation": {
            "violations": [e.case_id for e in violations],
            "unverifiable": [e.case_id for e in unverifiable],
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
        "rows": [
            {
                "case_id": e.case_id,
                "status": e.status,
                "reason": e.reason,
                "source_separation": e.source_separation,
                "example_path": e.example_path,
            }
            for e in evaluations
        ],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
