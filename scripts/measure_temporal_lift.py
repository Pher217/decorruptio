"""Measure temporal lift — does register-snapshot evidence move controls up the ladder?

This is an instrument-property measurement, NOT a re-run of the gold
benchmark and NOT a tuning pass. The benchmark thresholds are pre-registered
and locked; this script never touches them. It classifies the same 30
positive controls (`run_positive_controls.py`'s cohort) and 200 negative
controls (`run_negative_controls.py`'s cohort) on the evidence ladder
(`uncorrupt.graph.register_snapshots.EvidenceLevel`) and reports how many
move between levels once register-snapshot Attestations exist in the graph
(added by `ingest_register_snapshots.py`) versus without them.

Report positives and negatives SEPARATELY — always. If snapshots lift the
positives (move rows from level 3/4 to level 1/2) but ALSO lift the
negatives' level-1/2 rate, that is a false-positive generator and must be
reported loudly, not buried in an aggregate. A bare zero is never reported
without a confidence interval: 0/200 is not a 0% rate (Wilson 95% upper
bound ~1.9%), and 0/30 is not a 0% rate either.

Usage:
    # First run — establishes the current classification, save as a baseline
    # BEFORE running ingest_register_snapshots.py:
    PYTHONPATH=.:src python scripts/measure_temporal_lift.py \\
        --out experiments/temporal_lift_before.json

    # ... run scripts/ingest_register_snapshots.py to backfill snapshots ...

    # Second run — compare against the saved baseline to report the lift:
    PYTHONPATH=.:src python scripts/measure_temporal_lift.py \\
        --baseline experiments/temporal_lift_before.json \\
        --out experiments/temporal_lift_after.json

Requires `.consult/vip_lane_positives.csv` (the DHSC High Priority Lane
cohort — the same file `phase_c_paths.py` and `run_positive_controls.py`
use) and a populated graph. Neither is a bundled fixture: this script
resolves against real ingested data, same as its sibling scripts.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from datetime import date

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from scripts.phase_c_paths import (  # noqa: E402
    AWARD_CUTOFF,
    COHORT_CSV,
    VIP_CH_CACHE,
    _names_a_person,
    build_adjacency,
    resolve_referrer,
    resolve_supplier,
    surname,
)

from uncorrupt.graph.models import Entity  # noqa: E402
from uncorrupt.graph.register_snapshots import (  # noqa: E402
    EvidenceLevel,
    is_pre_award_admissible,
    relationship_evidence_level,
    wilson_interval,
)


def classify_positive_controls(
    adj: dict[int, list],
    people_by_surname: dict[str, list[Entity]],
    ch_cache: dict,
    max_hops: int,
    award_cutoff: date,
) -> list[dict]:
    """Classify the DHSC VIP-lane cohort on the evidence ladder.

    Mirrors `phase_c_paths.py`'s resolution loop exactly (same supplier/
    referrer resolution, same referrer-column fallback) but classifies with
    `relationship_evidence_level` instead of the strict pre-award-only
    `find_paths` — an unresolved row is reported as such, never silently
    dropped or counted as NO_TRACE (that would conflate poor matching with a
    real negative finding).
    """
    rows: list[dict] = []
    with open(COHORT_CSV, encoding="utf-8") as f:
        cohort = list(csv.DictReader(f))

    for row in cohort:
        supplier_name = (row.get("supplier_name") or "").strip()
        referrer_name = (row.get("source_of_referral") or "").strip()
        referrer_field = "source_of_referral"
        if not _names_a_person(referrer_name):
            fallback = (row.get("actual_referrer") or "").strip()
            if _names_a_person(fallback):
                referrer_name, referrer_field = fallback, "actual_referrer"

        if not _names_a_person(referrer_name):
            rows.append(
                {
                    "supplier": supplier_name,
                    "referrer": referrer_name,
                    "referrer_field": referrer_field,
                    "status": "referrer_not_a_person",
                    "level": None,
                }
            )
            continue

        supplier = resolve_supplier(supplier_name, ch_cache, row.get("company_number"))
        referrers = resolve_referrer(referrer_name, people_by_surname)

        if not supplier or not referrers:
            rows.append(
                {
                    "supplier": supplier_name,
                    "referrer": referrer_name,
                    "referrer_field": referrer_field,
                    "status": "unresolved",
                    "supplier_resolved": bool(supplier),
                    "referrer_candidates": len(referrers),
                    "level": None,
                }
            )
            continue

        level = relationship_evidence_level(
            {r.id for r in referrers}, supplier.id, adj, max_hops, award_cutoff
        )
        rows.append(
            {
                "supplier": supplier_name,
                "supplier_entity": supplier.name,
                "referrer": referrer_name,
                "referrer_field": referrer_field,
                "referrer_candidates": len(referrers),
                "status": "classified",
                "level": int(level),
                "level_name": level.name,
            }
        )

    return rows


def classify_negative_controls(
    adj: dict[int, list],
    n: int,
    max_hops: int,
    award_cutoff: date,
) -> list[dict]:
    """Classify random, non-adjacent person/company pairs on the evidence ladder.

    Same deterministic sampling as `run_negative_controls.py` (strided,
    reproducible, no seed) — the pool and pairing method must stay identical
    across the two scripts or the "spurious rate" comparison is meaningless.
    """
    from uncorrupt.graph.models import Edge

    people = list(
        Entity.objects.filter(
            entity_type="person", registry_scheme="UK-PARLIAMENT-MEMBER"
        ).order_by("id")
    )
    companies = list(
        Entity.objects.filter(entity_type="company", registry_scheme="GB-COH").order_by("id")
    )
    if not people or not companies:
        raise SystemExit("no candidate pool — is the graph populated?")

    pairs = []
    i = 0
    while len(pairs) < n and i < n * 20:
        person = people[(i * 7) % len(people)]
        company = companies[(i * 13) % len(companies)]
        i += 1
        if (
            Edge.objects.filter(source_entity=person, target_entity=company).exists()
            or Edge.objects.filter(source_entity=company, target_entity=person).exists()
        ):
            continue
        pairs.append((person, company))

    rows = []
    for person, company in pairs:
        level = relationship_evidence_level({person.id}, company.id, adj, max_hops, award_cutoff)
        rows.append(
            {
                "person": person.name,
                "company": company.name,
                "status": "classified",
                "level": int(level),
                "level_name": level.name,
            }
        )
    return rows


def level_distribution(rows: list[dict]) -> dict[str, int]:
    counts = Counter(r["level_name"] for r in rows if r.get("level") is not None)
    return {level.name: counts.get(level.name, 0) for level in EvidenceLevel}


def pre_award_admissible_count(rows: list[dict]) -> int:
    return sum(
        1
        for r in rows
        if r.get("level") is not None and is_pre_award_admissible(EvidenceLevel(r["level"]))
    )


def report_cohort(name: str, rows: list[dict]) -> dict:
    classified = [r for r in rows if r.get("level") is not None]
    n = len(classified)
    dist = level_distribution(rows)
    admissible = pre_award_admissible_count(rows)
    lower, upper = wilson_interval(admissible, n) if n else (0.0, 0.0)

    print(f"\n=== {name} (n={n} classified, {len(rows) - n} unresolved/excluded) ===")
    for level in EvidenceLevel:
        print(f"  {level.name:26s}: {dist[level.name]}")
    print(
        f"  pre-award admissible (1+2): {admissible}/{n}  "
        f"(95% CI [{lower * 100:.1f}%, {upper * 100:.1f}%])"
    )

    return {
        "n": n,
        "distribution": dist,
        "pre_award_admissible": admissible,
        "pre_award_admissible_ci_pct": [lower * 100, upper * 100],
    }


def report_lift(label: str, baseline: dict, current: dict) -> None:
    before = baseline["pre_award_admissible"]
    after = current["pre_award_admissible"]
    delta = after - before
    print(f"\n--- {label}: lift vs baseline ---")
    print(f"  pre-award admissible: {before} -> {after} (delta {delta:+d})")
    for level in EvidenceLevel:
        b = baseline["distribution"][level.name]
        c = current["distribution"][level.name]
        if b != c:
            print(f"  {level.name:26s}: {b} -> {c} (delta {c - b:+d})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-negative", type=int, default=200)
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--out", default="experiments/temporal_lift.json")
    parser.add_argument(
        "--baseline",
        default=None,
        help="Path to a previously-saved --out JSON to diff against (the lift).",
    )
    args = parser.parse_args()

    with open(VIP_CH_CACHE, encoding="utf-8") as f:
        ch_cache = json.load(f)

    people_by_surname: dict[str, list[Entity]] = defaultdict(list)
    for person in Entity.objects.filter(entity_type="person"):
        sn = surname(person.name)
        if sn:
            people_by_surname[sn].append(person)

    adj = build_adjacency()
    print(f"graph: {Entity.objects.count()} entities")

    positive_rows = classify_positive_controls(
        adj, people_by_surname, ch_cache, args.max_hops, AWARD_CUTOFF
    )
    negative_rows = classify_negative_controls(adj, args.n_negative, args.max_hops, AWARD_CUTOFF)

    positive_report = report_cohort("POSITIVE CONTROLS", positive_rows)
    negative_report = report_cohort("NEGATIVE CONTROLS", negative_rows)

    if negative_report["pre_award_admissible"] > 0:
        print(
            "\n*** WARNING: snapshot/date evidence makes at least one negative control "
            "pre-award-admissible. This is a false-positive generator — do not treat the "
            "positive-control lift as clean until this is investigated. ***"
        )

    baseline_data = None
    if args.baseline:
        with open(args.baseline, encoding="utf-8") as f:
            baseline_data = json.load(f)
        report_lift("POSITIVE CONTROLS", baseline_data["positive"], positive_report)
        report_lift("NEGATIVE CONTROLS", baseline_data["negative"], negative_report)

    output = {
        "award_cutoff": AWARD_CUTOFF.isoformat(),
        "max_hops": args.max_hops,
        "positive": positive_report,
        "negative": negative_report,
        "positive_rows": positive_rows,
        "negative_rows": negative_rows,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
