"""Measure the spec A2.4.3 per-material-stratum gates, write
experiments/stratum_gates.json.

`scripts/run_gold_benchmark.py` (owned elsewhere, read-only from here) loads
`experiments/stratum_gates.json` via `load_stratum_gates` but explicitly does
not measure it -- see that script's own "SCOPE" docstring section. Before
this script existed, that file was simply absent, so every material stratum
defaulted to `available=False` and REFUTED/CONFIRMED/PARTIAL could never be
reached -- only INSTRUMENT-LIMITED. This script is the missing producer.

Measures four strata (spec A2.4.3; the fourth is extra, see below), each via
its own wired `scripts/run_*_controls.py` control-battery runner (REUSED,
never reimplemented -- see `uncorrupt.gates.stratum._measure_wired_stratum`):
  * `lords_declared_interest`   -- RETRIEVAL measured live via
    `scripts/run_lords_controls.py`'s 12-row control battery (spec v2.9).
    Temporal is INTENTIONALLY left unmeasured (`None`, never a ratio) --
    see `uncorrupt.gates.stratum.measure_lords_stratum`'s docstring for why
    this makes a Lords temporal pass structurally impossible, not merely
    unlikely.
  * `ch_officer_appointment`, `commons_declared_interest` -- retrieval AND
    temporal now measured live via `scripts/run_ch_controls.py` /
    `scripts/run_commons_controls.py`'s own 12-row external control
    batteries. Either still reports `available=False` -- fail closed
    (ADR-008) -- if its fixture is missing, its runner cannot execute, OR
    the battery it ran against has fewer than
    `uncorrupt.gates.stratum.MIN_CONTROL_BATTERY_SIZE` (12) rows; otherwise
    `available=True` with the real measured score, whether that score
    passes the >=9/10 bar or not. `--ch-controls`/`--commons-controls`
    below point at a DIFFERENT fixture, not an arbitrarily SMALL one -- the
    battery-size floor exists precisely because these are free CLI paths
    (an independent review demonstrated a 1-row fixture otherwise reporting
    `available=True, passed=True` against a stratum whose real 12-row
    battery FAILS), and `--out`'s `control_fixtures_hash` binds the
    artifact to exactly which fixture bytes were used, auditable
    independently of the floor.
  * `electoral_commission` -- NOT one of `run_gold_benchmark.MATERIAL_STRATA`
    at all. Measured and reported anyway because the sealed gold cohort
    contains cases whose evidence stratum is `electoral_commission`, and a
    review found this means a `donation` edge can ride, completely
    unvalidated by any control, alongside a passing Companies House edge on
    a mixed path and still qualify for CONFIRMED/PARTIAL -- see
    `uncorrupt.gates.stratum.donation_edges_are_ungated_in_scorer`. This
    script prints an explicit warning when that condition holds; the JSON
    output is otherwise inert to `load_stratum_gates` (MATERIAL_STRATA has
    only 3 entries), by design -- fixing it requires amending
    `run_gold_benchmark.py`, out of this deliverable's scope.

Usage:
    PYTHONPATH=.:src python scripts/measure_stratum_gates.py \\
        --manifest data/gold_manifest.csv \\
        --lords-controls tests/fixtures/lords_retrieval_controls.json \\
        --out experiments/stratum_gates.json
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
    MATERIAL_STRATA,
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
    donation_edges_are_ungated_in_scorer,
    measure_all_strata,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--manifest", default="data/gold_manifest.csv")
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--lords-controls", default=DEFAULT_LORDS_CONTROLS_PATH)
    parser.add_argument("--ch-controls", default=DEFAULT_CH_CONTROLS_PATH)
    parser.add_argument("--commons-controls", default=DEFAULT_COMMONS_CONTROLS_PATH)
    parser.add_argument("--ec-controls", default=DEFAULT_EC_CONTROLS_PATH)
    parser.add_argument("--out", default="experiments/stratum_gates.json")
    parser.add_argument("--certificate-out", default="experiments/stratum_gates_certificate.json")
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
        # Binds this artifact to the EXACT fixture bytes used below -- closes
        # the "fixture is unbound" gap an independent review found: none of
        # the four fields above change when a control-battery fixture is
        # edited in the working tree (see uncorrupt.gates.binding's module
        # docstring). Computed from the SAME --*-controls paths passed to
        # measure_all_strata just below, never a separate, driftable read.
        control_fixtures_hash=compute_control_fixtures_hash(
            lords_controls_path=args.lords_controls,
            ch_controls_path=args.ch_controls,
            commons_controls_path=args.commons_controls,
            ec_controls_path=args.ec_controls,
        ),
    )

    print("=== STRATUM GATE MEASUREMENT (spec A2.4.3) ===")
    strata = measure_all_strata(
        lords_controls_path=args.lords_controls,
        ch_controls_path=args.ch_controls,
        commons_controls_path=args.commons_controls,
        ec_controls_path=args.ec_controls,
        max_hops=args.max_hops,
    )
    for name, measurement in strata.items():
        in_scorer = (
            "" if name in MATERIAL_STRATA else "  [NOT in run_gold_benchmark.MATERIAL_STRATA]"
        )
        print(
            f"{name}{in_scorer}: available={measurement.available} "
            f"retrieval={measurement.retrieval_recovered}/{measurement.retrieval_total} "
            f"temporal={measurement.temporal_recovered}/{measurement.temporal_total} -- "
            f"{'PASS' if measurement.passed else 'FAIL'}"
        )
        if measurement.note:
            print(f"  {measurement.note}")

    if donation_edges_are_ungated_in_scorer():
        print(
            "\nWARNING: donation edges carry no material stratum in "
            "run_gold_benchmark.MATERIAL_STRATA -- a mixed path (e.g. one officer_of edge "
            "plus one donation edge) can qualify for CONFIRMED/PARTIAL through the "
            "Companies House gate alone, with the donation edge's own evidence completely "
            "unvalidated by any control. The sealed cohort contains cases in exactly this "
            "position (12597000, 08126173)."
        )

    report = {
        **freeze_state.to_binding_dict(),
        **{
            # to_gate_dict()'s five fields first, UNCHANGED -- this is the exact
            # contract run_gold_benchmark.load_stratum_gates reads via
            # entry.get(...) calls, so adding note/extra as sibling keys is
            # purely additive (verified: it ignores unknown keys, never rejects
            # them). Without these two, a reader of stratum_gates.json sees
            # e.g. retrieval_total=12 beside temporal_total=12 with nothing
            # explaining WHICH fixture, at what path, produced them.
            name: {
                **measurement.to_gate_dict(),
                "note": measurement.note,
                "extra": measurement.extra,
            }
            for name, measurement in strata.items()
        },
        "electoral_commission_note": (
            "not consumed by run_gold_benchmark.load_stratum_gates (MATERIAL_STRATA has only "
            "3 entries) -- present here for audit and for the no-score certificate only. See "
            "uncorrupt.gates.stratum.donation_edges_are_ungated_in_scorer."
        ),
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")

    certificate = build_no_score_certificate(freeze_state, stratum_measurements=strata)
    if certificate is not None:
        cert_path = write_no_score_certificate(args.certificate_out, certificate)
        print(f"\n>>> NO SCORE: {len(certificate['blockers'])} gate(s) blocked scoring <<<")
        for blocker in certificate["blockers"]:
            print(f"  BLOCKED: {blocker['gate']} -- {blocker['reason']}")
        print(f"wrote {cert_path}")
    else:
        print("\n>>> all measured stratum gates passed -- no certificate emitted <<<")


if __name__ == "__main__":
    main()
