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
rows are structurally unrecoverable vs. merely not-yet-ingested), the
threshold-arithmetic table that makes "nine successes regardless of
denominator" an impossible misreading of ">=9/10", the sealed cohort's
identity and its explicit not-scored status, and a `strata_measured` block
naming every stratum's real score -- including a PASSING one (Electoral
Commission), because a certificate that hides the one passing stratum is as
dishonest as one that hides the failures.

`--manifest` is used only to bind `manifest_hash` -- it is NEVER read to
select, score, or otherwise touch the sealed gold cohort (cohort identity
comes from the hard-coded `SEALED_COHORT_V2_COMPANY_NUMBERS` constant, not
from this file). If no manifest file exists at that path, `manifest_hash` is
recorded as an explicit "UNAVAILABLE: ..." string rather than silently
omitted or fabricated -- ADR-008's own standard applied to this script's own
output, not just to the gates it reports on.

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
    compute_control_fixtures_hash,
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
    "UNAVAILABLE" marker otherwise. Cohort identity does not depend on this
    value -- see `SEALED_COHORT_V2_COMPANY_NUMBERS` -- but ADR-008 requires
    every frozen-state field to be accounted for, and a missing input must
    be an auditable statement, never a silent gap or an invented hash."""
    if manifest_path.exists():
        return compute_manifest_hash(manifest_path)
    return (
        f"UNAVAILABLE: no file at {manifest_path} in this environment -- cohort identity is "
        "bound instead via the hard-coded SEALED_COHORT_V2_COMPANY_NUMBERS constant "
        "(scripts/run_gold_benchmark.py), not via this hash. Recorded explicitly rather than "
        "omitted or fabricated (ADR-008: a silent gap is the same failure class as everything "
        "else this project has been fighting)."
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

    return {
        "total_controls": total,
        "structurally_blocked_rows": structurally_blocked,
        "structurally_blocked_count": blocked,
        "max_achievable_recovered": ceiling,
        "max_achievable_fraction": ceiling_fraction,
        "max_achievable_pct": round(100 * ceiling_fraction, 1),
        "gate_fraction": GATE_FRACTION,
        "gate_pct": GATE_FRACTION * 100,
        "ceiling_passes_gate": ceiling_fraction >= GATE_FRACTION,
        "finding": (
            f"{blocked} of {total} Companies House controls cite a company_number with no "
            "staging.Company row at all (legacy/non-file identifiers absent from the "
            "BasicCompanyDataAsOneFile bulk CSV, which gates company-entity creation) -- no "
            f"amount of further officer ingestion can ever resolve these rows. Maximum "
            f"achievable score is {ceiling}/{total} ({round(100 * ceiling_fraction, 1)}%), "
            f"still below the {GATE_FRACTION * 100:.0f}% gate "
            f"({'PASSES' if ceiling_fraction >= GATE_FRACTION else 'FAILS'})."
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
        "statement": (
            "The sealed 20-case gold cohort (spec 'SEALED COHORT v2', selection salt "
            "'decorruptio-gold-cohort-v1:') was NOT scored and remains unspent. The "
            "pre-registered stop rule (spec amendment v2.10) fired before any gold row was "
            "evaluated: the Companies House control battery's structural ceiling "
            f"({ceiling['max_achievable_recovered']}/{ceiling['total_controls']} = "
            f"{ceiling['max_achievable_pct']}%) cannot reach the {GATE_FRACTION * 100:.0f}% "
            "readiness gate under any amount of further ingestion, so scoring never proceeded."
        ),
    }
    certificate["verdict"] = "NO SCORE -- INSTRUMENT-LIMITED"

    cert_path = write_no_score_certificate(args.out, certificate)
    print(f"\n>>> {certificate['verdict']}: wrote {cert_path} <<<")
    for blocker in certificate["blockers"]:
        print(f"  BLOCKED: {blocker['gate']} -- {blocker['reason']}")
    print(f"\n{certificate['sealed_cohort']['statement']}")


if __name__ == "__main__":
    main()
