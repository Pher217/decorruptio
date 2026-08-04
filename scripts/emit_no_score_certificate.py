"""Emit the ADR-008 no-score certificate for the terminated UK strict-endpoint
run (spec `phase-c-gold-manifest-preregistration.md` amendment v2.10).

The pre-registered stop rule has fired: the Companies House control battery
scores 7/12 against the spec A2.4.3 gate (`>=9/10`, i.e. 90%), and its own
structural ceiling -- two controls cite legacy `NF`-prefixed identifiers with
no row at all in the Companies House bulk CSV, so no graph `Entity` can ever
be created for them regardless of further ingestion -- is 10/12 (83.3%),
still below the gate. Per ADR-008 this MUST produce an auditable no-score
certificate naming exactly which control blocked scoring: *"we did not
score" is a finding, not a discretionary silence*.

This script does not reimplement measurement. It reuses, unmodified:
  * `scripts.run_gold_benchmark.compute_graph_hash` / `current_code_commit` /
    `compute_manifest_hash` / `SEALED_COHORT_V2_COMPANY_NUMBERS`
  * `uncorrupt.gates.binding.GateFreezeState` /
    `compute_attestation_inclusive_hash`
  * `uncorrupt.gates.stratum.measure_all_strata` /
    `compute_control_fixtures_hash` (the four control-battery runners
    `run_ch_controls.py` / `run_commons_controls.py` /
    `run_lords_controls.py` / `run_ec_controls.py`, wired there)
  * `uncorrupt.gates.certificate.build_no_score_certificate` /
    `write_no_score_certificate`

What this script adds, because the generic certificate builder does not
compute them: the Companies House structural-ceiling finding (which control
rows are structurally unrecoverable vs. merely not-yet-ingested, with its
prose DERIVED from `ceiling_passes_gate` rather than hard-coded, so a future
rerun after a general `NF -> live number` alias fix -- spec A2.10.3 -- cannot
print a self-contradiction like "100%, still below the gate"), the
threshold-arithmetic table that makes "nine successes regardless of
denominator" an impossible misreading of ">=9/10", the sealed cohort's
identity and its explicit not-scored status (`sealed_cohort_statement`,
likewise derived from the actual measured blockers rather than presuming
Companies House is the sole or permanent cause), a `strata_measured` block
naming every stratum's real score -- including a PASSING one (Electoral
Commission), because a certificate that hides the one passing stratum is as
dishonest as one that hides the failures -- and an
`electoral_commission_materiality` caveat making clear that pass is 0-of-3
MATERIAL gates, not 1-of-4, since Electoral Commission is not in
`run_gold_benchmark.MATERIAL_STRATA` and cannot gate or rescue scoring.

This certificate covers only the four STRATUM gates (spec A2.4.3), never the
separate coverage-gate family (spec A2.4.2,
`scripts/measure_coverage_gate.py`) -- flagged explicitly in the certificate
`note`, not silently out of scope.

`--manifest` is used only to bind `manifest_hash` -- it is NEVER read to
select, score, or otherwise touch the sealed gold cohort (cohort identity
comes from the hard-coded `SEALED_COHORT_V2_COMPANY_NUMBERS` constant, not
from this file). If no manifest file exists at that path, `manifest_hash` is
recorded as an explicit "UNAVAILABLE: ..." string rather than silently
omitted or fabricated. What actually keeps cohort identity auditable in that
case is not `SEALED_COHORT_V2_COMPANY_NUMBERS`/`code_commit` binding (a bare
`git rev-parse HEAD` has no dirty-tree check) but that `sealed_cohort`
publishes all 20 company numbers inline, independently checkable against the
spec's own "SEALED COHORT v2" table by a reader holding only this file.

Usage:
    PYTHONPATH=.:src python scripts/emit_no_score_certificate.py
    PYTHONPATH=.:src python scripts/emit_no_score_certificate.py \\
        --out experiments/no_score_certificate.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from scripts.run_gold_benchmark import (  # noqa: E402
    SEALED_COHORT_V2_COMPANY_NUMBERS,
    compute_graph_hash,
    compute_manifest_hash,
    current_code_commit,
)

from uncorrupt.gates.binding import (  # noqa: E402
    GateFreezeState,
    compute_attestation_inclusive_hash,
    utc_now_iso,
)
from uncorrupt.gates.certificate import (  # noqa: E402
    build_no_score_certificate,
    write_no_score_certificate,
)
from uncorrupt.gates.stratum import (  # noqa: E402
    DEFAULT_CH_CONTROLS_PATH,
    DEFAULT_COMMONS_CONTROLS_PATH,
    DEFAULT_EC_CONTROLS_PATH,
    DEFAULT_LORDS_CONTROLS_PATH,
    StratumMeasurement,
    compute_control_fixtures_hash,
    donation_edges_are_ungated_in_scorer,
    measure_all_strata,
)
from uncorrupt.graph.models import Edge, Entity  # noqa: E402
from uncorrupt.staging.companies_house import normalise_company_number  # noqa: E402
from uncorrupt.staging.models import Company  # noqa: E402

# spec A2.4.3's ">=9/10 per material stratum" -- 90%, not "nine successes
# regardless of denominator". Misreading it that way was a real error made
# during this run (spec amendment v2.10 §A2.10.1) and this script's own
# threshold_arithmetic table exists specifically so that misreading cannot
# recur unnoticed.
GATE_FRACTION = 0.9

DEFAULT_OUT_PATH = "experiments/no_score_certificate.json"


def _manifest_hash_or_unavailable(manifest_path: Path) -> str:
    """Real hash if the manifest exists; an explicit, non-fabricated
    "UNAVAILABLE" marker otherwise.

    A missing manifest hash here does NOT weaken cohort-identity
    auditability, but not for the reason a `code_commit`/constant-binding
    argument would suggest: `current_code_commit()` is a bare `git
    rev-parse HEAD` with no dirty-tree check, so it cannot by itself prove
    `SEALED_COHORT_V2_COMPANY_NUMBERS` (scripts/run_gold_benchmark.py)
    matches what was actually measured -- an uncommitted edit to that
    constant would leave `code_commit` unchanged. What actually carries the
    guarantee is that `sealed_cohort` below publishes all 20 company
    numbers INLINE, in this JSON file, independently checkable by any
    reader holding only this certificate and the spec's own "SEALED COHORT
    v2" table -- stronger than a hash for this specific claim, since a hash
    proves nothing to a reader who cannot recompute it, while a published
    list can be compared by eye. ADR-008 still requires every frozen-state
    field to be accounted for, so a missing manifest file is recorded
    explicitly here rather than omitted or fabricated.
    """
    if manifest_path.exists():
        return compute_manifest_hash(manifest_path)
    return (
        f"UNAVAILABLE: no file at {manifest_path} in this environment. Cohort identity for "
        "this certificate is independently checkable anyway -- see `sealed_cohort` below, "
        "which publishes all 20 company numbers inline against spec 'SEALED COHORT v2' -- "
        "recorded explicitly here rather than omitted or fabricated (ADR-008)."
    )


def ch_structural_ceiling(
    ch_controls_path: str | Path = DEFAULT_CH_CONTROLS_PATH,
) -> dict[str, Any]:
    """Independently recompute spec v2.10 A2.10.1's structural-ceiling finding.

    For each of the 12 externally-sourced CH controls, checks whether a
    `staging.Company` row exists at all for its `company_number` -- i.e.
    whether that identifier appears anywhere in the ingested
    BasicCompanyDataAsOneFile bulk CSV, the sole gate on graph company-entity
    creation (`uncorrupt.graph.ch_officers`/`ch_appointments` only ever
    resolve a company via `Company.objects.filter(company_number=...)`).

    A control whose `Company` row is ABSENT can never be recovered by any
    amount of further officer/appointment ingestion -- that is the
    structural ceiling. A control whose `Company` row EXISTS but which still
    failed today is a coverage gap, not a structural one, and is excluded
    from the "structurally blocked" count.
    """
    controls = json.loads(Path(ch_controls_path).read_text(encoding="utf-8"))["controls"]
    structurally_blocked = []
    for control in controls:
        raw_number = control["company_number"]
        normalised = normalise_company_number(raw_number)
        if not Company.objects.filter(company_number=normalised).exists():
            structurally_blocked.append(
                {
                    "id": control.get("id"),
                    "company_number": raw_number,
                    "company_name": control.get("company_name"),
                    "reason": (
                        "no staging.Company row for this company_number -- absent from the "
                        "ingested Companies House bulk CSV entirely, a legacy/non-file "
                        "identifier the current architecture cannot resolve"
                    ),
                }
            )

    total = len(controls)
    blocked = len(structurally_blocked)
    ceiling = total - blocked
    ceiling_fraction = (ceiling / total) if total else 0.0
    ceiling_passes_gate = ceiling_fraction >= GATE_FRACTION
    # Derived from ceiling_passes_gate, never asserted -- a prior version of
    # this string hard-coded "still below the gate" regardless of the actual
    # number, so re-running after a hypothetical general NF-alias fix (spec
    # v2.10 A2.10.3) that raises the ceiling to a PASSING score printed the
    # self-contradiction "12/12 (100.0%), still below the 90% gate (PASSES)".
    # Both halves of the sentence must now come from the same boolean.
    gate_relation = (
        f"clears the {GATE_FRACTION * 100:.0f}% gate"
        if ceiling_passes_gate
        else f"is still below the {GATE_FRACTION * 100:.0f}% gate"
    )

    return {
        "total_controls": total,
        "structurally_blocked_rows": structurally_blocked,
        "structurally_blocked_count": blocked,
        "max_achievable_recovered": ceiling,
        "max_achievable_fraction": ceiling_fraction,
        "max_achievable_pct": round(100 * ceiling_fraction, 1),
        "gate_fraction": GATE_FRACTION,
        "gate_pct": GATE_FRACTION * 100,
        "ceiling_passes_gate": ceiling_passes_gate,
        "finding": (
            f"{blocked} of {total} Companies House controls cite a company_number with no "
            "staging.Company row at all (legacy/non-file identifiers absent from the "
            "BasicCompanyDataAsOneFile bulk CSV, which gates company-entity creation) -- no "
            f"amount of further officer ingestion can ever resolve these rows. Maximum "
            f"achievable score is {ceiling}/{total} ({round(100 * ceiling_fraction, 1)}%), "
            f"which {gate_relation} ({'PASSES' if ceiling_passes_gate else 'FAILS'})."
        ),
    }


def threshold_arithmetic_table(
    total: int, gate_fraction: float = GATE_FRACTION
) -> list[dict[str, Any]]:
    """Every possible score out of `total`, with its percentage and whether it
    clears `gate_fraction` -- so ">=9/10" cannot be misread as "nine
    successes regardless of denominator" (the exact error spec v2.10
    §A2.10.1 named and corrected)."""
    return [
        {
            "recovered": recovered,
            "total": total,
            "pct": round(100 * recovered / total, 1),
            "passes_gate": (recovered / total) >= gate_fraction,
        }
        for recovered in range(total + 1)
    ]


def sealed_cohort_statement(ceiling: dict[str, Any], certificate: dict[str, Any]) -> str:
    """Compose the sealed-cohort not-scored statement from what was actually
    measured -- `certificate["blockers"]` (whichever gates
    `build_no_score_certificate` found failing) and the CH structural
    ceiling -- rather than presuming the Companies House ceiling is the sole
    or permanent cause of NO SCORE.

    Today it is (Commons and Lords are ALSO independently failing, but CH is
    the one with a proven structural, not merely coverage, ceiling). But
    spec A2.10.3 explicitly contemplates a general `NF -> live number` alias
    layer that could raise the CH ceiling above the gate on a future rerun.
    If that happens, this function must describe CH as no longer the
    blocker rather than keep asserting a "cannot reach the gate" clause a
    passing ceiling would make false -- exactly the coordinator-caught bug
    in the previous, hard-coded version of this sentence.
    """
    blocking_gates = [b["gate"] for b in certificate["blockers"]]
    gate_pct = f"{GATE_FRACTION * 100:.0f}%"
    ceiling_summary = (
        f"{ceiling['max_achievable_recovered']}/{ceiling['total_controls']} = "
        f"{ceiling['max_achievable_pct']}%"
    )

    if ceiling["ceiling_passes_gate"]:
        ch_clause = (
            f"the Companies House control battery's structural ceiling ({ceiling_summary}) "
            f"clears the {gate_pct} readiness gate, so it does not by itself block scoring"
        )
    else:
        ch_clause = (
            f"the Companies House control battery's structural ceiling ({ceiling_summary}) "
            f"cannot reach the {gate_pct} readiness gate under any amount of further ingestion"
        )

    return (
        "The sealed 20-case gold cohort (spec 'SEALED COHORT v2', selection salt "
        "'decorruptio-gold-cohort-v1:') was NOT scored and remains unspent. The "
        "pre-registered stop rule (spec amendment v2.10) fired before any gold row was "
        f"evaluated: {ch_clause}. As measured, {len(blocking_gates)} gate(s) currently block "
        f"scoring ({', '.join(blocking_gates) if blocking_gates else 'none'}); see `blockers` "
        "for the exact recovered/total score behind each -- scoring proceeds only once every "
        "material stratum passes both retrieval and temporal (spec A2.4.4)."
    )


def electoral_commission_materiality_note(ec_measurement: StratumMeasurement) -> dict[str, Any]:
    """Electoral Commission is measured and reported alongside the three
    material strata, and can pass -- but it is NOT one of
    `run_gold_benchmark.MATERIAL_STRATA`, so naming it as a pass without
    this caveat reads as "1 of 4 gates passing" when the materially correct
    framing is "0 of 3 material gates passing" (today, all three material
    strata fail).

    Calls `uncorrupt.gates.stratum.donation_edges_are_ungated_in_scorer` --
    which exists, per its own docstring, "so the no-score certificate can
    name it", but which this script had never actually called until now.
    """
    ungated = donation_edges_are_ungated_in_scorer()
    mechanism = (
        (
            "A mixed path (one officer_of edge plus one donation edge) can currently qualify "
            "for CONFIRMED/PARTIAL through the Companies House gate alone, with the donation "
            "edge's own evidence completely unvalidated by any control -- the sealed cohort "
            "contains cases in exactly this position (12597000, 08126173). "
        )
        if ungated
        else ""
    )
    return {
        "in_material_strata": not ungated,
        "measured_recovered": ec_measurement.retrieval_recovered,
        "measured_total": ec_measurement.retrieval_total,
        "passed": ec_measurement.passed,
        "caveat": (
            f"Electoral Commission scored {ec_measurement.retrieval_recovered}/"
            f"{ec_measurement.retrieval_total} today, but is NOT one of "
            "run_gold_benchmark.MATERIAL_STRATA -- it neither gates scoring nor could rescue a "
            "failing material stratum. `strata_measured` alone reads as '1 of 4 strata "
            "passing'; the materially correct framing is '0 of 3 material gates passing'. "
            f"{mechanism}"
            "See uncorrupt.gates.stratum.donation_edges_are_ungated_in_scorer."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--manifest", default="data/gold_manifest.csv")
    parser.add_argument("--lords-controls", default=DEFAULT_LORDS_CONTROLS_PATH)
    parser.add_argument("--ch-controls", default=DEFAULT_CH_CONTROLS_PATH)
    parser.add_argument("--commons-controls", default=DEFAULT_COMMONS_CONTROLS_PATH)
    parser.add_argument("--ec-controls", default=DEFAULT_EC_CONTROLS_PATH)
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--out", default=DEFAULT_OUT_PATH)
    args = parser.parse_args()

    print("=== NO-SCORE CERTIFICATE EMISSION (ADR-008, spec amendment v2.10) ===")
    print(f"graph: {Entity.objects.count()} entities, {Edge.objects.count()} edges")

    freeze_state = GateFreezeState(
        code_commit=current_code_commit(),
        graph_hash=compute_graph_hash(),
        attestation_inclusive_hash=compute_attestation_inclusive_hash(),
        manifest_hash=_manifest_hash_or_unavailable(Path(args.manifest)),
        measured_at=utc_now_iso(),
        control_fixtures_hash=compute_control_fixtures_hash(
            lords_controls_path=args.lords_controls,
            ch_controls_path=args.ch_controls,
            commons_controls_path=args.commons_controls,
            ec_controls_path=args.ec_controls,
        ),
    )

    print("\n--- measuring all four strata (spec A2.4.3) ---")
    strata = measure_all_strata(
        lords_controls_path=args.lords_controls,
        ch_controls_path=args.ch_controls,
        commons_controls_path=args.commons_controls,
        ec_controls_path=args.ec_controls,
        max_hops=args.max_hops,
    )
    for name, m in strata.items():
        print(
            f"{name}: available={m.available} "
            f"retrieval={m.retrieval_recovered}/{m.retrieval_total} "
            f"temporal={m.temporal_recovered}/{m.temporal_total} -- "
            f"{'PASS' if m.passed else 'FAIL'}"
        )

    ceiling = ch_structural_ceiling(args.ch_controls)
    print(f"\n--- Companies House structural ceiling ---\n{ceiling['finding']}")

    certificate = build_no_score_certificate(freeze_state, stratum_measurements=strata)
    if certificate is None:
        # Every measured stratum passed -- there is genuinely nothing to
        # certify as no-score. Refuse rather than silently doing nothing:
        # a caller expecting NO SCORE here (spec v2.10 says CH fails) needs
        # to know its inputs disagree with the pre-registered finding, not
        # get a silent no-op.
        raise SystemExit(
            "REFUSING to emit a no-score certificate: every measured stratum passed. This "
            "contradicts spec amendment v2.10 (CH 7/12 measured, below the 90% gate) -- check "
            "--ch-controls/--commons-controls/--lords-controls/--ec-controls point at the real "
            "fixtures and the graph has not changed since v2.10 was written."
        )

    certificate["strata_measured"] = {
        name: {
            "available": m.available,
            "retrieval_recovered": m.retrieval_recovered,
            "retrieval_total": m.retrieval_total,
            "temporal_recovered": m.temporal_recovered,
            "temporal_total": m.temporal_total,
            "passed": m.passed,
            "note": m.note,
        }
        for name, m in strata.items()
    }
    certificate["ch_structural_ceiling"] = ceiling
    certificate["threshold_arithmetic"] = {
        "gate": (
            f">= 9/10 (i.e. {GATE_FRACTION * 100:.0f}%) per spec A2.4.3 -- NOT 'nine successes "
            "regardless of denominator'. Reading it that way was a real error made during this "
            "run (spec amendment v2.10 A2.10.1) and is the reason this table exists."
        ),
        "ch_battery_size_12": threshold_arithmetic_table(ceiling["total_controls"]),
    }
    certificate["sealed_cohort"] = {
        "cohort_size": len(SEALED_COHORT_V2_COMPANY_NUMBERS),
        "company_numbers": sorted(SEALED_COHORT_V2_COMPANY_NUMBERS),
        "scored": False,
        "statement": sealed_cohort_statement(ceiling, certificate),
    }
    certificate["electoral_commission_materiality"] = electoral_commission_materiality_note(
        strata["electoral_commission"]
    )
    # This certificate covers only the four STRATUM gates (spec A2.4.3).
    # Appended, not replacing build_no_score_certificate's own note: a
    # future run where all three material strata pass would emit NO
    # certificate from this script at all, which is not the same claim as
    # "ready to score" -- the separate coverage-gate family (spec A2.4.2,
    # scripts/measure_coverage_gate.py) still has to be checked.
    certificate["note"] += (
        " This certificate measures only the four material/extra STRATUM gates (spec A2.4.3) "
        "-- Companies House, Commons, Lords, Electoral Commission. It does NOT include the "
        "separate coverage-gate family (spec A2.4.2: supplier-universe / Commons-universe "
        "ingest completeness, produced by scripts/measure_coverage_gate.py) -- a hypothetical "
        "future run where all three material strata pass would still need that script's own "
        "certificate to rule out a coverage-gate failure before scoring; this artifact alone "
        "emitting no certificate is not the same claim as 'ready to score'."
    )
    certificate["verdict"] = "NO SCORE -- INSTRUMENT-LIMITED"

    cert_path = write_no_score_certificate(args.out, certificate)
    print(f"\n>>> {certificate['verdict']}: wrote {cert_path} <<<")
    for blocker in certificate["blockers"]:
        print(f"  BLOCKED: {blocker['gate']} -- {blocker['reason']}")
    print(f"\n{certificate['sealed_cohort']['statement']}")


if __name__ == "__main__":
    main()
