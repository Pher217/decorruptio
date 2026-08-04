"""Measure temporal lift — does register-snapshot evidence move controls up the ladder?

This is an instrument-property measurement, NOT a re-run of the gold
benchmark and NOT a tuning pass. The benchmark thresholds are pre-registered
and locked; this script never touches them. It classifies the 30 positive
controls (`run_positive_controls.py`'s cohort — see `classify_positive_controls`)
and 200 negative controls (`run_negative_controls.py`'s cohort) on the
evidence ladder (`uncorrupt.graph.register_snapshots.EvidenceLevel`) and
reports how many move between levels once register-snapshot Attestations
exist in the graph (added by `ingest_register_snapshots.py`) versus without
them.

CORRECTNESS NOTE (fixed after a live run caught it — see the commit that
introduced this fix): the positive cohort here MUST be `run_positive_controls.py`'s
30 `declared_interest` edges, sampled directly from the graph
(`scripts/run_positive_controls.py:92-99`) and re-resolved from strings the
same way that script does (`as_published` + surname/name matching) — NOT the
DHSC VIP-lane referral cohort (`.consult/vip_lane_positives.csv`). The
VIP-lane cohort was formally ruled an invalid positive set ("made a
referral" is not evidence of a pre-existing relationship); measuring
temporal lift on it answers nothing about the actual control gate. It is
still classified here (`classify_vip_lane_cohort`) and reported, but under a
name that can never be mistaken for the real 30-control gate — this
project's most repeated defect is testing the wrong cohort / wrong
denominator (see the commit titled "Phase C tested the wrong referrer
column and the wrong denominator"; locked spec §7.3 exists because of it).

Report every cohort SEPARATELY — always. If snapshots lift the positives
(move rows from level 3/4 to level 1/2) but ALSO lift the negatives'
level-1/2 rate, that is a false-positive generator and must be reported
loudly, not buried in an aggregate. A bare zero is never reported without a
confidence interval: 0/200 is not a 0% rate (Wilson 95% upper bound ~1.9%),
and 0/30 is not a 0% rate either.

ALPHABETICAL-COVERAGE BIAS: the Lords register is sorted alphabetically and
Wayback's archival density is NOT uniform across it — page 1 has ~51 unique
captures (2020-06 to 2026-07), page 2 has ~14, page 5 ~7, page 20 ~4, page 40
~6 (see `uncorrupt.graph.register_snapshots.describe_page_coverage_bias`).
Any lift from snapshot evidence is therefore confounded by a member's
surname position, not just their true relationship history. This script
reports lift broken down by register page for every positive control that
has a directly-sampled `declared_interest` edge (`report_page_bias`) — a
lift number without this breakdown is not trustworthy on its own.

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

The VIP-lane classification additionally requires `.consult/vip_lane_positives.csv`
and is skipped (with a note, not an error) if that file is absent — it is
reported for continuity only and is never required for the real control gate.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from scripts.phase_c_paths import (  # noqa: E402
    AWARD_CUTOFF,
    COHORT_CSV,
    VIP_CH_CACHE,
    _names_a_person,
    build_adjacency,
    normalise_name,
    resolve_referrer,
    resolve_supplier,
    surname,
)
from scripts.run_positive_controls import as_published  # noqa: E402

from uncorrupt.graph.models import Edge, Entity  # noqa: E402
from uncorrupt.graph.register_snapshots import (  # noqa: E402
    EvidenceLevel,
    describe_page_coverage_bias,
    edge_evidence_level,
    is_pre_award_admissible,
    relationship_evidence_level,
    snapshot_evidence_pages,
    wilson_interval,
)

# The real control-gate cohort size (see run_positive_controls.py and the
# commit history it was actually run with — "positives 28/30 retrieved").
# run_positive_controls.py's own CLI default (--n 10) is a lighter smoke
# value; 30 is the number this project reports as "the positive controls".
POSITIVE_CONTROL_N = 30


def classify_positive_controls(
    adj: dict[int, list],
    people_by_surname: dict[str, list[Entity]],
    max_hops: int,
    award_cutoff: date,
    n: int = POSITIVE_CONTROL_N,
) -> list[dict]:
    """Classify the REAL 30 positive controls — `run_positive_controls.py`'s
    cohort — on the evidence ladder.

    Reuses that script's exact selection VERBATIM
    (`scripts/run_positive_controls.py:92-99`: `declared_interest` edges
    with a UK-PARLIAMENT-MEMBER source and a GB-COH target, ordered by edge
    id, deterministic, no seed) and its exact re-expression/re-resolution
    method (`as_published` title-stripping, then surname + exact-name
    resolution FROM STRINGS ONLY — never reusing the entity the edge was
    sampled from). This can't be imported instead of duplicated: that
    script defines the query and resolution inline in `main()`, not as a
    separate function, and this task's scope forbids editing that file to
    extract one. If either changes, change it in BOTH places, or the two
    "30 positive controls" numbers will silently diverge — exactly the
    wrong-cohort/wrong-denominator failure this project has hit repeatedly.

    Classifies with `relationship_evidence_level` (the full ladder) instead
    of `find_paths` (dated-only) so a pre-award snapshot can promote a
    control `run_positive_controls.py` itself would report as undated. An
    unresolved row is reported as such, never silently dropped or treated
    as NO_TRACE — poor matching must never masquerade as a real finding.

    Each classified row also carries `source_edge_level`/`source_edge_pages`
    — the evidence level and register page(s) of the ORIGINAL sampled edge
    itself (not the re-resolved retrieval path), used only for the
    alphabetical-coverage-bias breakdown in `report_page_bias`.
    """
    candidates = (
        Edge.objects.filter(
            edge_type="declared_interest",
            target_entity__registry_scheme="GB-COH",
            source_entity__registry_scheme="UK-PARLIAMENT-MEMBER",
        )
        .select_related("source_entity", "target_entity")
        .order_by("id")[:n]
    )

    rows: list[dict] = []
    for edge in candidates:
        person = edge.source_entity
        company = edge.target_entity
        published_name = as_published(person.name)

        person_candidates = people_by_surname.get(surname(published_name), [])
        target = normalise_name(company.name)
        nearby = Entity.objects.filter(
            entity_type="company", name__icontains=company.name.strip()[:15]
        )[:200]
        company_matches = [e for e in nearby if normalise_name(e.name) == target]

        source_edge_level = edge_evidence_level(edge, award_cutoff)
        source_edge_pages = snapshot_evidence_pages(edge, award_cutoff)

        if not person_candidates or len(company_matches) != 1:
            rows.append(
                {
                    "person_register_name": person.name,
                    "company": company.name,
                    "status": "unresolved",
                    "level": None,
                    "source_edge_level": int(source_edge_level),
                    "source_edge_pages": source_edge_pages,
                }
            )
            continue

        goal = company_matches[0]
        level = relationship_evidence_level(
            {p.id for p in person_candidates}, goal.id, adj, max_hops, award_cutoff
        )
        if level is None:
            # A structural path exists but none of it is temporally
            # meaningful (every path found is same_as-only) — see
            # `relationship_evidence_level`'s docstring. Reuses this file's
            # existing "level": None convention for excluded-from-count rows
            # (see `report_cohort`'s `r.get("level") is not None` filter)
            # rather than a second sentinel.
            rows.append(
                {
                    "person_register_name": person.name,
                    "person_as_published": published_name,
                    "company": company.name,
                    "status": "classified_no_temporal_claim",
                    "level": None,
                    "source_edge_level": int(source_edge_level),
                    "source_edge_pages": source_edge_pages,
                }
            )
            continue
        rows.append(
            {
                "person_register_name": person.name,
                "person_as_published": published_name,
                "company": company.name,
                "status": "classified",
                "level": int(level),
                "level_name": level.name,
                "source_edge_level": int(source_edge_level),
                "source_edge_pages": source_edge_pages,
            }
        )

    return rows


def classify_vip_lane_cohort(
    adj: dict[int, list],
    people_by_surname: dict[str, list[Entity]],
    ch_cache: dict,
    max_hops: int,
    award_cutoff: date,
) -> list[dict]:
    """Classify the DHSC VIP-lane referral cohort — INVALID as a positive
    set, reported for continuity only.

    "Made a referral" is not evidence of a pre-existing relationship, which
    is exactly why the gold-manifest/positive-controls exercise exists —
    see `run_positive_controls.py`'s own docstring. This function must
    NEVER be reported under the "positive controls" name; the caller labels
    it "VIP-LANE COHORT (invalid positive set, reported for continuity
    only)" so it can't be mistaken for the real control gate again.

    Mirrors `phase_c_paths.py`'s resolution loop exactly (same supplier/
    referrer resolution, same referrer-column fallback) but classifies with
    `relationship_evidence_level` instead of the strict pre-award-only
    `find_paths`.
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
        if level is None:
            rows.append(
                {
                    "supplier": supplier_name,
                    "supplier_entity": supplier.name,
                    "referrer": referrer_name,
                    "referrer_field": referrer_field,
                    "referrer_candidates": len(referrers),
                    "status": "classified_no_temporal_claim",
                    "level": None,
                }
            )
            continue
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
        if level is None:
            rows.append(
                {
                    "person": person.name,
                    "company": company.name,
                    "status": "classified_no_temporal_claim",
                    "level": None,
                }
            )
            continue
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


def report_page_bias(rows: list[dict], describe_live: bool) -> None:
    """Break down PRE_AWARD_OBSERVED promotions by register page — the
    alphabetical-coverage confound, reported plainly rather than averaged
    away. `rows` must carry `source_edge_level`/`source_edge_pages` (only
    `classify_positive_controls` rows do — VIP-lane and negative-control
    rows have no single sampled declared_interest edge to trace a page
    from, so they're outside this breakdown's scope).
    """
    promoted = [
        r
        for r in rows
        if r.get("source_edge_level") == int(EvidenceLevel.PRE_AWARD_OBSERVED)
        and r.get("source_edge_pages")
    ]
    print(f"\n--- ALPHABETICAL-COVERAGE BIAS (n={len(rows)} sampled edges) ---")
    if not promoted:
        print(
            "  no edges were promoted to PRE_AWARD_OBSERVED by a snapshot — nothing to break down"
        )
    else:
        page_counts: Counter[int] = Counter()
        for row in promoted:
            for page in row["source_edge_pages"]:
                page_counts[page] += 1
        print(f"  {len(promoted)}/{len(rows)} sampled edges promoted by a snapshot")
        print("  promotions by register page:")
        for page in sorted(page_counts):
            print(f"    page {page:2d}: {page_counts[page]}")
        if all(page <= 2 for page in page_counts):
            print(
                "  *** every promotion came from page 1-2 (the densely-archived, "
                "alphabetically-earliest slice) — do not generalise this lift to the "
                "full register. ***"
            )

    if describe_live:
        sample_pages = sorted(
            {1, 2, 5, 10, 20, 30, 40} | {p for r in rows for p in r.get("source_edge_pages", [])}
        )
        print(f"\n  live Wayback capture density for pages {sample_pages}:")
        coverage = describe_page_coverage_bias(sample_pages)
        for page, count in coverage.items():
            print(f"    page {page:2d}: {count} unique captures")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-positive", type=int, default=POSITIVE_CONTROL_N)
    parser.add_argument("--n-negative", type=int, default=200)
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--out", default="experiments/temporal_lift.json")
    parser.add_argument(
        "--baseline",
        default=None,
        help="Path to a previously-saved --out JSON to diff against (the lift).",
    )
    parser.add_argument(
        "--describe-page-coverage",
        action="store_true",
        help="Also query the live Wayback CDX API for capture density per register page "
        "(network call) alongside the page-bias breakdown.",
    )
    parser.add_argument(
        "--skip-vip-lane",
        action="store_true",
        help="Skip the invalid VIP-lane continuity classification entirely.",
    )
    args = parser.parse_args()

    people_by_surname: dict[str, list[Entity]] = defaultdict(list)
    for person in Entity.objects.filter(entity_type="person"):
        sn = surname(person.name)
        if sn:
            people_by_surname[sn].append(person)

    adj = build_adjacency()
    print(f"graph: {Entity.objects.count()} entities")

    positive_rows = classify_positive_controls(
        adj, people_by_surname, args.max_hops, AWARD_CUTOFF, n=args.n_positive
    )
    negative_rows = classify_negative_controls(adj, args.n_negative, args.max_hops, AWARD_CUTOFF)

    positive_report = report_cohort("POSITIVE CONTROLS", positive_rows)
    negative_report = report_cohort("NEGATIVE CONTROLS", negative_rows)
    report_page_bias(positive_rows, describe_live=args.describe_page_coverage)

    vip_lane_rows: list[dict] = []
    vip_lane_report: dict | None = None
    if not args.skip_vip_lane:
        if Path(VIP_CH_CACHE).exists() and Path(COHORT_CSV).exists():
            with open(VIP_CH_CACHE, encoding="utf-8") as f:
                ch_cache = json.load(f)
            vip_lane_rows = classify_vip_lane_cohort(
                adj, people_by_surname, ch_cache, args.max_hops, AWARD_CUTOFF
            )
            vip_lane_report = report_cohort(
                "VIP-LANE COHORT (invalid positive set, reported for continuity only)",
                vip_lane_rows,
            )
        else:
            print(
                f"\n(skipping VIP-lane continuity classification — {COHORT_CSV} or "
                f"{VIP_CH_CACHE} not found; it is never required for the real control gate)"
            )

    if negative_report["pre_award_admissible"] > 0:
        print(
            "\n*** WARNING: snapshot/date evidence makes at least one negative control "
            "pre-award-admissible. This is a false-positive generator — do not treat the "
            "positive-control lift as clean until this is investigated. ***"
        )

    if args.baseline:
        with open(args.baseline, encoding="utf-8") as f:
            baseline_data = json.load(f)
        report_lift("POSITIVE CONTROLS", baseline_data["positive"], positive_report)
        report_lift("NEGATIVE CONTROLS", baseline_data["negative"], negative_report)
        if vip_lane_report is not None and "vip_lane" in baseline_data:
            report_lift(
                "VIP-LANE COHORT (invalid positive set)", baseline_data["vip_lane"], vip_lane_report
            )

    output = {
        "award_cutoff": AWARD_CUTOFF.isoformat(),
        "max_hops": args.max_hops,
        "positive": positive_report,
        "negative": negative_report,
        "vip_lane": vip_lane_report,
        "positive_rows": positive_rows,
        "negative_rows": negative_rows,
        "vip_lane_rows": vip_lane_rows,
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
