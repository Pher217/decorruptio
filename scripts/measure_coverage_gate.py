"""Measure the spec A2.4.2 coverage gate and write experiments/coverage_gate.json.

`scripts/run_gold_benchmark.py` (owned elsewhere, read-only from here) loads
`experiments/coverage_gate.json` via `load_coverage_gate` but explicitly does
not measure it -- see that script's own "SCOPE" docstring section. Before
this script existed, that file was simply absent, so `CoverageGate` always
defaulted to its all-`None` (failing) state and a run could only ever reach
INVALID by construction. This script is the missing producer.

Measures three coverage checks (spec A2.4.2, delegation packet):
  * Companies House officer-roster coverage over the procurement-supplier
    universe (`uncorrupt.gates.coverage.measure_ch_officer_coverage`).
  * UK Parliament (Commons) register ingest completeness against the live
    Interests API's own `totalResults`
    (`uncorrupt.gates.coverage.measure_commons_coverage`).
  * Lords members/interests ingested vs. a frozen, hash-verified register
    snapshot, if `--lords-snapshot-dir` is supplied
    (`uncorrupt.gates.coverage.measure_lords_snapshot_coverage`) --
    informational only: `CoverageGate` has no field for Lords at all (see
    that class's definition), so this never affects
    `supplier_universe_covered`/`commons_universe_covered`. The GATING Lords
    measurement lives in `stratum_gates.json`'s `lords_declared_interest`
    entry, produced by `scripts/measure_stratum_gates.py`.

THE THRESHOLD IS NOT 90%. `run_gold_benchmark.CoverageGate.passed`
independently computes its own `covered/total >= 90%` from the two raw
counts this script writes -- a standard an independent review judged
defensible only for a pre-registered external CONTROL sample, never for
census-style ingestion coverage ("if 10% is missing, the missingness may be
systematically concentrated among precisely the difficult suppliers"). This
script's own `strict_gate` block in the output, and the no-score certificate
it emits on failure, apply the correct 100%-accounted standard instead (see
`uncorrupt.gates.coverage`'s module docstring) -- independently of whatever
the downstream 90% check would compute.

Usage:
    PYTHONPATH=.:src python scripts/measure_coverage_gate.py \\
        --manifest data/gold_manifest.csv \\
        --ch-output-dir experiments/ch_officers \\
        --lords-snapshot-dir /path/to/lords-snapshot \\
        --out experiments/coverage_gate.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from scripts.run_gold_benchmark import (  # noqa: E402
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
from uncorrupt.gates.coverage import (  # noqa: E402
    DEFAULT_CH_OUTPUT_DIR,
    CoverageMeasurement,
    measure_ch_officer_coverage,
    measure_commons_coverage,
    measure_lords_snapshot_coverage,
)


def _print_measurement(label: str, m: CoverageMeasurement) -> None:
    print(
        f"{label}: {m.ingested} ingested, {m.explicitly_failed} explicitly failed, "
        f"{m.not_attempted} never attempted, of {m.total} -- "
        f"{'PASS' if m.passed else 'FAIL'} (100%-accounted standard, spec A2.4.2)"
    )
    for limit in m.known_limits:
        print(f"  KNOWN LIMIT: {limit}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--manifest", default="data/gold_manifest.csv")
    parser.add_argument("--ch-output-dir", default=DEFAULT_CH_OUTPUT_DIR)
    parser.add_argument(
        "--commons-total-override",
        type=int,
        default=None,
        help="skip the live Commons Interests API call and use this totalResults value "
        "instead (offline runs, or pinning a prior reading).",
    )
    parser.add_argument(
        "--lords-snapshot-dir",
        default=None,
        help="path to a frozen, hash-verified Lords register HTML snapshot directory "
        "(provenance.json + page_NN.html). If omitted, Lords snapshot coverage is not "
        "measured (reported as unmeasured, never as passing).",
    )
    parser.add_argument("--out", default="experiments/coverage_gate.json")
    parser.add_argument("--certificate-out", default="experiments/coverage_gate_certificate.json")
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    if not manifest_path.exists():
        raise SystemExit(
            f"--manifest {manifest_path} does not exist -- the freeze-state binding requires "
            "a real manifest hash (spec A2.4.5); refusing to measure against a fabricated one."
        )

    freeze_state = GateFreezeState(
        code_commit=current_code_commit(),
        graph_hash=compute_graph_hash(),
        attestation_inclusive_hash=compute_attestation_inclusive_hash(),
        manifest_hash=compute_manifest_hash(manifest_path),
        measured_at=utc_now_iso(),
    )

    print("=== COVERAGE GATE MEASUREMENT (spec A2.4.2) ===")

    ch = measure_ch_officer_coverage(args.ch_output_dir)
    _print_measurement("companies_house_officer_roster", ch)

    if args.commons_total_override is not None:
        commons = measure_commons_coverage(total_results=args.commons_total_override)
    else:
        print("querying live Commons Interests API for totalResults...")
        commons = measure_commons_coverage()
    _print_measurement("commons_register", commons)

    lords_members = lords_interests = None
    if args.lords_snapshot_dir:
        lords = measure_lords_snapshot_coverage(args.lords_snapshot_dir)
        lords_members, lords_interests = lords.members, lords.interests
        _print_measurement("lords_members (snapshot, informational only)", lords_members)
        _print_measurement("lords_interests (snapshot, informational only)", lords_interests)
    else:
        print(
            "lords: --lords-snapshot-dir not supplied -- Lords snapshot coverage not measured. "
            "CoverageGate has no field for Lords anyway; the gating Lords measurement is "
            "stratum_gates.json's lords_declared_interest (scripts/measure_stratum_gates.py)."
        )

    report: dict = {
        **freeze_state.to_binding_dict(),
        "supplier_universe_covered": ch.ingested,
        "supplier_universe_total": ch.total,
        "commons_universe_covered": commons.ingested,
        "commons_universe_total": commons.total,
        "strict_gate": {
            "standard": (
                "100% accounted for (spec A2.4.2, corrected from a 90% ratio -- "
                "see src/uncorrupt/gates/coverage.py module docstring)"
            ),
            "companies_house_officer_roster": {
                "passed": ch.passed,
                "ingested": ch.ingested,
                "explicitly_failed": ch.explicitly_failed,
                "not_attempted": ch.not_attempted,
                "total": ch.total,
                "failure_manifest_sample": list(ch.failure_manifest[:50]),
                "failure_manifest_truncated": len(ch.failure_manifest) > 50,
                "known_limits": list(ch.known_limits),
                "extra": ch.extra,
            },
            "commons_register": {
                "passed": commons.passed,
                "ingested": commons.ingested,
                "explicitly_failed": commons.explicitly_failed,
                "not_attempted": commons.not_attempted,
                "total": commons.total,
                "known_limits": list(commons.known_limits),
                "extra": commons.extra,
            },
        },
    }
    if lords_members is not None and lords_interests is not None:
        report["lords_snapshot"] = {
            "note": (
                "informational only -- CoverageGate has no consuming field for Lords; the "
                "gating Lords measurement is stratum_gates.json's lords_declared_interest."
            ),
            "members": {
                "passed": lords_members.passed,
                "ingested": lords_members.ingested,
                "not_attempted": lords_members.not_attempted,
                "total": lords_members.total,
            },
            "interests": {
                "passed": lords_interests.passed,
                "ingested": lords_interests.ingested,
                "explicitly_failed": lords_interests.explicitly_failed,
                "not_attempted": lords_interests.not_attempted,
                "total": lords_interests.total,
                "known_limits": list(lords_interests.known_limits),
            },
        }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")
    print(
        "\nNOTE: supplier_universe_covered/total and commons_universe_covered/total feed "
        "run_gold_benchmark.CoverageGate, which independently computes its OWN "
        "covered/total >= 90% check from these two raw counts -- a looser, out-of-scope "
        "standard this script cannot change (that file is owned elsewhere). This script's "
        "own 'strict_gate' block above is the correct spec-A2.4.2 100%-accounted "
        "determination, and is what the no-score certificate below is built from."
    )

    coverage_measurements = {"companies_house_officer_roster": ch, "commons_register": commons}
    if lords_members is not None and lords_interests is not None:
        coverage_measurements["lords_members"] = lords_members
        coverage_measurements["lords_interests"] = lords_interests

    certificate = build_no_score_certificate(
        freeze_state, coverage_measurements=coverage_measurements
    )
    if certificate is not None:
        cert_path = write_no_score_certificate(args.certificate_out, certificate)
        print(f"\n>>> NO SCORE: {len(certificate['blockers'])} gate(s) blocked scoring <<<")
        for blocker in certificate["blockers"]:
            print(f"  BLOCKED: {blocker['gate']} -- {blocker['reason']}")
        print(f"wrote {cert_path}")
    else:
        print("\n>>> all measured coverage gates passed -- no certificate emitted <<<")


if __name__ == "__main__":
    main()
