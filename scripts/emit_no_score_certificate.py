"""Emit the ADR-008 no-score certificate for the terminated UK strict-endpoint
run (spec `phase-c-gold-manifest-preregistration.md` amendment v2.10).

The pre-registered stop rule has fired: the Companies House control battery
scores 7/12 against the spec A2.4.3 gate (`>=9/10`, i.e. 90%), and its own
current-pipeline ceiling -- two controls cite legacy `NF`-prefixed identifiers with
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
    `write_no_score_certificate` / `assert_all_required_families_accounted_for`

What this script adds, because the generic certificate builder does not
compute them: the Companies House current-pipeline-ceiling finding (which control
rows the current pipeline cannot resolve vs. merely not-yet-ingested, with its
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

This certificate also covers the coverage-gate family (spec A2.4.2:
supplier-universe / Commons-universe ingest completeness,
`scripts/measure_coverage_gate.py`) as a first-class, machine-readable part
of `blockers` -- fixing an earlier defect where that family was named only
in prose. `_load_coverage_gate_measurements` looks for a prior
`measure_coverage_gate.py` run's `experiments/coverage_gate.json` (reusing
its `strict_gate` block, never re-measuring) that is BOUND to this
certificate's own freeze state (same code_commit/graph_hash/manifest_hash);
if none exists, is stale, or is missing an expected family, that family is
recorded UNMEASURED -- which `build_no_score_certificate` treats as an
unconditional blocker, never a silent omission (ADR-008: unknown is not the
same claim as passing). `assert_all_required_families_accounted_for` is the
structural guarantee that this script can never compute a verdict while a
required family -- stratum or coverage -- was neither measured, marked
UNMEASURED, nor confirmed passing; see its docstring in
`uncorrupt.gates.certificate`.

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
    assert_all_required_families_accounted_for,
    build_no_score_certificate,
    write_no_score_certificate,
)
from uncorrupt.gates.coverage import CoverageMeasurement  # noqa: E402
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

# The two coverage-gate families spec A2.4.2 / `run_gold_benchmark.CoverageGate`
# actually gate on (`supplier_universe_covered`/`commons_universe_covered`).
# Lords snapshot coverage is deliberately excluded -- `CoverageGate` has no
# field for it at all (see `uncorrupt.gates.coverage`'s module docstring);
# it is informational only and never gates or blocks scoring.
REQUIRED_COVERAGE_FAMILIES = ("companies_house_officer_roster", "commons_register")

DEFAULT_COVERAGE_GATE_REPORT_PATH = "experiments/coverage_gate.json"


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
    amount of further officer/appointment ingestion. A control whose
    `Company` row EXISTS but which still failed today is a coverage gap and
    is excluded from the blocked count.

    NAMING, per spec amendment v2.11: this is a CURRENT-PIPELINE ceiling, not
    a proven structural limit of the registers. v2.10 called it "structural"
    while simultaneously holding (A2.10.3) that a general `NF -> live number`
    alias layer "would be a legitimate pipeline correction" -- a ceiling
    cannot be both structural and remediable, and the stronger reading was
    withdrawn. "Further INGESTION cannot lift this" is measured and stands;
    "no remediation can" was never established. The identifiers and function
    name are kept for continuity with previously published certificates; the
    emitted `characterisation` field carries the corrected claim.
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
                        "identifier THE CURRENT PIPELINE cannot resolve. Not established as "
                        "intrinsically unresolvable (spec v2.11)"
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
        "ceiling_kind": "CURRENT_PIPELINE",
        "characterisation": (
            "CURRENT-PIPELINE ceiling, not a proven structural limit (spec v2.11). Measured "
            "and standing: no amount of further officer/appointment INGESTION lifts this. NOT "
            "established: that no general, source-derived remediation could. A former-name "
            "alias layer was built and does NOT reach these rows -- NF/FC/SF is Companies "
            "House's oversea-company branch-registration scheme, not a rename record. The "
            "mechanism that might is foreign_company_details.registration_number, a published "
            "per-record cross-reference absent from the bulk CSV. Until that is built and run, "
            "'these rows are unresolvable' is UNVERIFIED and must be reported as such."
        ),
        "finding": (
            f"{blocked} of {total} Companies House controls cite a company_number with no "
            "staging.Company row at all (legacy/non-file identifiers absent from the "
            "BasicCompanyDataAsOneFile bulk CSV, which gates company-entity creation) -- no "
            f"amount of further officer ingestion can resolve these rows under the current "
            f"pipeline. Maximum achievable score is {ceiling}/{total} "
            f"({round(100 * ceiling_fraction, 1)}%), "
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
            f"the Companies House control battery's current-pipeline ceiling ({ceiling_summary}) "
            f"clears the {gate_pct} readiness gate, so it does not by itself block scoring"
        )
    else:
        ch_clause = (
            f"the Companies House control battery's current-pipeline ceiling ({ceiling_summary}) "
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


def _load_coverage_gate_measurements(
    report_path: Path, freeze_state: GateFreezeState
) -> tuple[dict[str, CoverageMeasurement], dict[str, str]]:
    """Load spec A2.4.2's coverage-gate family from a prior
    `scripts/measure_coverage_gate.py` run, if one exists and is BOUND to
    the same code/graph/manifest state this certificate is being emitted
    for.

    Reuses the `strict_gate` block `measure_coverage_gate.py` already
    writes -- reconstructing `CoverageMeasurement` from its own recorded
    counts, never re-measuring (this script has no coverage-measurement
    logic of its own).

    Returns `(measured, unmeasured_reasons)`. A required family
    (`REQUIRED_COVERAGE_FAMILIES`) lands in `unmeasured_reasons` -- never
    silently omitted -- when: no report file exists at all, the report's
    own binding (code_commit/graph_hash/manifest_hash -- the same three
    fields `run_gold_benchmark.GateBinding` checks) does not match this
    certificate's freeze state, or the report exists and is bound but does
    not carry that family's `strict_gate` entry. A stale coverage
    measurement -- one bound to a different code/graph/manifest state -- is
    treated as unmeasured for THIS certificate, never silently trusted
    (spec A2.4.5).
    """
    if not report_path.exists():
        reason = (
            f"no coverage-gate report at {report_path} -- scripts/measure_coverage_gate.py "
            "has not been run against this environment (spec A2.4.2)."
        )
        return {}, dict.fromkeys(REQUIRED_COVERAGE_FAMILIES, reason)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    bound = (
        report.get("code_commit") == freeze_state.code_commit
        and report.get("graph_hash") == freeze_state.graph_hash
        and report.get("manifest_hash") == freeze_state.manifest_hash
    )
    if not bound:
        reason = (
            f"coverage-gate report at {report_path} is bound to a different code/graph/"
            f"manifest state (code_commit={report.get('code_commit')!r}, "
            f"graph_hash={report.get('graph_hash')!r}) than this certificate "
            f"(code_commit={freeze_state.code_commit!r}, graph_hash={freeze_state.graph_hash!r}) "
            "-- a stale coverage measurement is never trusted for a different state (spec "
            "A2.4.5)."
        )
        return {}, dict.fromkeys(REQUIRED_COVERAGE_FAMILIES, reason)

    strict_gate = report.get("strict_gate", {})
    measured: dict[str, CoverageMeasurement] = {}
    unmeasured: dict[str, str] = {}
    for name in REQUIRED_COVERAGE_FAMILIES:
        entry = strict_gate.get(name)
        if entry is None:
            unmeasured[name] = (
                f"coverage-gate report at {report_path} is bound to this state but carries no "
                f"'{name}' entry in its strict_gate block."
            )
            continue
        measured[name] = CoverageMeasurement(
            name=name,
            ingested=entry["ingested"],
            explicitly_failed=entry["explicitly_failed"],
            not_attempted=entry["not_attempted"],
            total=entry["total"],
            failure_manifest=tuple(entry.get("failure_manifest_sample", ())),
            known_limits=tuple(entry.get("known_limits", ())),
            extra=entry.get("extra", {}),
        )
    return measured, unmeasured


def coverage_gate_note(
    coverage_measurements: dict[str, CoverageMeasurement], unmeasured_coverage: dict[str, str]
) -> str:
    """Compose the certificate note's coverage-gate-family sentence from
    what was actually loaded/measured THIS run, never a fixed claim -- a
    certificate whose prose says a family is "not included" while its own
    `blockers` names a `coverage:*` entry (or vice versa) would be exactly
    the prose/data disagreement an independent review already caught once
    in this file (see `ch_structural_ceiling`'s docstring for the earlier
    instance of that defect class).
    """
    parts = []
    for name in REQUIRED_COVERAGE_FAMILIES:
        if name in unmeasured_coverage:
            parts.append(f"{name}: UNMEASURED ({unmeasured_coverage[name]})")
        else:
            m = coverage_measurements[name]
            parts.append(
                f"{name}: {m.accounted_for}/{m.total} accounted for -- "
                f"{'PASS' if m.passed else 'FAIL'}"
            )
    return (
        " This certificate also covers the coverage-gate family (spec A2.4.2: "
        "supplier-universe / Commons-universe ingest completeness, produced by "
        "scripts/measure_coverage_gate.py) as a first-class, machine-readable part of "
        "`blockers` -- it is never silently absent. As measured this run: "
        f"{'; '.join(parts)}. An UNMEASURED family blocks scoring exactly like a FAIL: "
        "unknown is never treated as passing (ADR-008)."
    )


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
    parser.add_argument("--coverage-gate-report", default=DEFAULT_COVERAGE_GATE_REPORT_PATH)
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
    print(f"\n--- Companies House current-pipeline ceiling ---\n{ceiling['finding']}")

    stratum_certificate = build_no_score_certificate(freeze_state, stratum_measurements=strata)
    if stratum_certificate is None:
        # Every measured stratum passed -- there is genuinely nothing to
        # certify as no-score. Refuse rather than silently doing nothing:
        # a caller expecting NO SCORE here (spec v2.10 says CH fails) needs
        # to know its inputs disagree with the pre-registered finding, not
        # get a silent no-op. This check is deliberately STRATUM-ONLY: an
        # UNMEASURED coverage-gate family below would also make the
        # combined certificate non-None, which must never mask this
        # specific refusal (a stratum-passing state disagreeing with spec
        # v2.10) behind an unrelated coverage blocker.
        raise SystemExit(
            "REFUSING to emit a no-score certificate: every measured stratum passed. This "
            "contradicts spec amendment v2.10 (CH 7/12 measured, below the 90% gate) -- check "
            "--ch-controls/--commons-controls/--lords-controls/--ec-controls point at the real "
            "fixtures and the graph has not changed since v2.10 was written."
        )

    print("\n--- coverage-gate family (spec A2.4.2) ---")
    coverage_measurements, unmeasured_coverage = _load_coverage_gate_measurements(
        Path(args.coverage_gate_report), freeze_state
    )
    for name, reason in unmeasured_coverage.items():
        print(f"coverage:{name}: UNMEASURED -- {reason}")
    for name, m in coverage_measurements.items():
        print(
            f"coverage:{name}: {m.accounted_for}/{m.total} accounted for -- "
            f"{'PASS' if m.passed else 'FAIL'}"
        )

    certificate = build_no_score_certificate(
        freeze_state,
        stratum_measurements=strata,
        coverage_measurements=coverage_measurements,
        unmeasured_families={
            f"coverage:{name}": reason for name, reason in unmeasured_coverage.items()
        },
    )
    # Structural guarantee (closes the fail-closed hole this script used to
    # have): refuse to proceed unless EVERY required family -- all four
    # strata plus both required coverage families -- was explicitly
    # measured-and-failed, measured-and-passed, or marked UNMEASURED. Given
    # `stratum_certificate` above already proved at least one stratum
    # blocker exists, `certificate` here is never `None`; this assertion is
    # the enforcement point, not a redundant check -- a future edit that
    # forgets to pass one of these inputs trips it immediately.
    assert_all_required_families_accounted_for(
        required_gate_names=(
            {f"stratum:{name}" for name in strata}
            | {f"coverage:{name}" for name in REQUIRED_COVERAGE_FAMILIES}
        ),
        certificate=certificate,
        passed_families=(
            {f"stratum:{name}" for name, m in strata.items() if m.available and m.passed}
            | {f"coverage:{name}" for name, m in coverage_measurements.items() if m.passed}
        ),
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
    certificate["coverage_gate_measured"] = {
        name: {
            "ingested": m.ingested,
            "explicitly_failed": m.explicitly_failed,
            "not_attempted": m.not_attempted,
            "total": m.total,
            "passed": m.passed,
        }
        for name, m in coverage_measurements.items()
    }
    certificate["coverage_gate_unmeasured"] = dict(unmeasured_coverage)
    # Appended, not replacing build_no_score_certificate's own note --
    # DERIVED from what was actually loaded/measured this run
    # (coverage_gate_note), never a fixed claim. This replaces a prior
    # version of this sentence that hard-coded "does NOT include" the
    # coverage-gate family regardless of what `blockers` actually
    # contained -- exactly the prose/data disagreement this file's own
    # `ch_structural_ceiling` docstring already names as a defect class.
    certificate["note"] += coverage_gate_note(coverage_measurements, unmeasured_coverage)
    certificate["verdict"] = "NO SCORE -- INSTRUMENT-LIMITED"

    cert_path = write_no_score_certificate(args.out, certificate)
    print(f"\n>>> {certificate['verdict']}: wrote {cert_path} <<<")
    for blocker in certificate["blockers"]:
        print(f"  BLOCKED: {blocker['gate']} -- {blocker['reason']}")
    print(f"\n{certificate['sealed_cohort']['statement']}")


if __name__ == "__main__":
    main()
