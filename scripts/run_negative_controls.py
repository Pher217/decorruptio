"""Negative controls — how often does a 2-hop path appear by chance?

Positive controls prove the pipeline CAN recover a known relationship
(29/30). They say nothing about whether a recovered path MEANS anything. If
random person/company pairs are connected within two hops at a high rate, then
a path between a referrer and a supplier is unremarkable and the whole
approach is measuring graph density rather than influence.

This is the base-rate discipline the project already applies to indicators
(an indicator firing on >20% of a source's units is suppressed as
non-discriminating) applied to path search.

Method: sample person/company pairs that have NO direct edge between them,
drawn from the same entity pools the real test draws from, and run the
identical path search. The reported rate is the probability that a 2-hop path
exists between two entities with no recorded relationship.

Interpretation:
  * a low rate means a recovered path is informative
  * a high rate means paths are structural noise, and any positive result from
    Phase C would need a precision estimate before it could be believed

Pairs are drawn deterministically (ordered by id, strided) rather than with a
random seed, so the negative set cannot be resampled until it flatters a
result.

THIS NUMBER EXPIRES. It measured 0/200 on a graph of 29,818 edges whose
`officer_of` layer is a near-star topology (21k officers, 1k companies, few
shared directors). Ingesting the officer-appointment second hop will add edges
that connect companies to each other through shared people, which is precisely
what raises 2-hop reachability. Re-run this after every ingest before quoting
any Phase C hit rate as a signal -- a spurious rate measured on a sparser
graph flatters a denser one.

Usage:
    PYTHONPATH=.:src python scripts/run_negative_controls.py --n 200
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from scripts.phase_c_paths import build_adjacency, find_paths  # noqa: E402

from uncorrupt.graph.models import Edge, Entity  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=200)
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--out", default="experiments/negative_controls.json")
    args = parser.parse_args()

    # Draw from the same pools the real test uses: people who appear in a
    # register (not the 21k CH officers, who would bias the estimate towards
    # the officer subgraph) and companies resolved to Companies House.
    people = list(
        Entity.objects.filter(
            entity_type="person", registry_scheme="UK-PARLIAMENT-MEMBER"
        ).order_by("id")
    )
    companies = list(
        Entity.objects.filter(
            entity_type="company", registry_scheme="GB-COH"
        ).order_by("id")
    )
    if not people or not companies:
        raise SystemExit("no candidate pool — is the graph populated?")

    # Deterministic stride, so the sample is reproducible and cannot be
    # redrawn until it looks better.
    pairs = []
    i = 0
    while len(pairs) < args.n and i < args.n * 20:
        person = people[(i * 7) % len(people)]
        company = companies[(i * 13) % len(companies)]
        i += 1
        # A "negative" must have no DIRECT edge either way.
        if Edge.objects.filter(
            source_entity=person, target_entity=company
        ).exists() or Edge.objects.filter(
            source_entity=company, target_entity=person
        ).exists():
            continue
        pairs.append((person, company))

    adj = build_adjacency()
    print(f"graph: {Entity.objects.count()} entities, {Edge.objects.count()} edges")
    print(f"negative pairs sampled: {len(pairs)}")

    with_path = 0
    with_preaward = 0
    rows = []
    for person, company in pairs:
        dated, undated = find_paths({person.id}, company.id, adj, args.max_hops, date.max)
        pre_award, _ = find_paths({person.id}, company.id, adj, args.max_hops)
        found = bool(dated or undated)
        if found:
            with_path += 1
        if pre_award:
            with_preaward += 1
        rows.append(
            {
                "person": person.name,
                "company": company.name,
                "path_found": found,
                "path_count": len(dated) + len(undated),
                "preaward": bool(pre_award),
            }
        )

    n = len(pairs)
    rate = 100 * with_path / n if n else 0.0
    print(f"\n=== NEGATIVE CONTROLS (n={n}, max {args.max_hops} hops) ===")
    print(f"spurious path found       : {with_path}/{n}  ({rate:.1f}%)")
    print(f"spurious pre-award path   : {with_preaward}/{n}")
    print(
        "\nA path between a referrer and a supplier is only informative to the\n"
        "extent this rate is low. Compare it against the Phase C hit rate\n"
        "before treating any recovered path as a signal."
    )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(
            {
                "n": n,
                "with_path": with_path,
                "with_preaward": with_preaward,
                "spurious_rate_pct": rate,
                "rows": rows[:100],
            },
            f,
            indent=2,
        )
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
