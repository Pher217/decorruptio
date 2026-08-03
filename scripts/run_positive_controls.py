"""Positive controls — prove the retrieval pipeline can recover a KNOWN link.

Phase C v1 reported 0 of 52 and I believed it. Sol's review showed the cohort
was never a positive-labelled set ("made a referral" is not evidence of a
pre-existing relationship), so that zero measured nothing about the hypothesis.
Worse, the project already had a standing rule -- *prove the pipeline can
produce a non-null on a known-good case before believing any null* -- and it
had been applied to ingest bugs but never to the benchmark itself.

This script is that missing control. It samples relationships that are
provably IN the graph, re-expresses them as the plain strings an external
table would carry (titles stripped, company suffixes intact), and then asks the
pipeline to recover them **from those strings alone**, using the same
resolution and path-search code Phase C uses. If a known-present relationship
cannot be recovered, the retrieval layer is broken and every null it has ever
produced is uninterpretable.

Two numbers are reported, and the gap between them is the point:

  retrieved         -- a path was found at all (tests resolution + search)
  retrieved_preaward -- that path was also temporally admissible

The second will be near zero for register-of-interests data by construction:
only 0.4% of `declared_interest` edges carry a `valid_from`, because the Lords
register publishes no start dates. That is a property of the SOURCE, not a
finding about relationships, and conflating the two is exactly how Phase C v1
produced a zero that felt like evidence.

Selection is deterministic (ordered by edge id, no sampling seed) so the
control set cannot be reshuffled until it flatters the result.

Usage:
    PYTHONPATH=.:src python scripts/run_positive_controls.py --n 10
"""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import defaultdict
from datetime import date

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from scripts.phase_c_paths import (  # noqa: E402
    build_adjacency,
    find_paths,
    normalise_name,
    prefer_companies_house,
    surname,
)

from uncorrupt.graph.models import Edge, Entity  # noqa: E402

# Titles that appear in the register but never in an external table like the
# DHSC High Priority Lane list. Stripping them is what makes the control a
# genuine test of resolution rather than a string equality check.
_TITLE_RE = re.compile(
    r"^(The\s+)?(Rt\s+Hon\s+)?(Lord|Lady|Baroness|Baron|Sir|Dame|Earl|Viscount|"
    r"Duke|Duchess|Bishop|Archbishop|Dr|Mr|Mrs|Ms)\s+",
    re.IGNORECASE,
)
_OF_SUFFIX_RE = re.compile(r"\s+of\s+.+$", re.IGNORECASE)


def as_published(person_name: str) -> str:
    """Re-express a register name the way an external table would write it.

    "The Lord Archbishop of York" -> "York"; "Baroness Mone of Mayfair" ->
    "Mone". This deliberately DISCARDS information the register has and the
    external source does not, so the control exercises the same weak matching
    Phase C must survive.
    """
    name = _TITLE_RE.sub("", (person_name or "").strip())
    name = _OF_SUFFIX_RE.sub("", name)
    return name.strip() or person_name


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=10)
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--out", default="experiments/positive_controls.json")
    args = parser.parse_args()

    # Controls must be relationships that are unambiguously in the graph AND
    # resolvable to a registry identifier on the company side.
    candidates = (
        Edge.objects.filter(
            edge_type="declared_interest",
            target_entity__registry_scheme="GB-COH",
            source_entity__registry_scheme="UK-PARLIAMENT-MEMBER",
        )
        .select_related("source_entity", "target_entity")
        .order_by("id")[: args.n]
    )

    people_by_surname: dict[str, list[Entity]] = defaultdict(list)
    for person in Entity.objects.filter(entity_type="person"):
        sn = surname(person.name)
        if sn:
            people_by_surname[sn].append(person)

    adj = build_adjacency()
    print(f"graph: {Entity.objects.count()} entities, {Edge.objects.count()} edges")

    results = []
    retrieved = 0
    retrieved_preaward = 0

    for edge in candidates:
        person = edge.source_entity
        company = edge.target_entity
        published_name = as_published(person.name)

        # Resolve from strings only — never reuse the entity we sampled from.
        person_candidates = people_by_surname.get(surname(published_name), [])
        target = normalise_name(company.name)
        nearby = Entity.objects.filter(
            entity_type="company", name__icontains=company.name.strip()[:15]
        )[:200]
        company_matches = prefer_companies_house(
            [e for e in nearby if normalise_name(e.name) == target]
        )

        row = {
            "person_register_name": person.name,
            "person_as_published": published_name,
            "company": company.name,
            "company_number": company.company_number,
            "person_candidates": len(person_candidates),
            "company_candidates": len(company_matches),
        }

        if not person_candidates or len(company_matches) != 1:
            row["status"] = "unresolved"
            results.append(row)
            continue

        goal = company_matches[0]
        # RETRIEVAL = a path exists at all, dated or not. `find_paths` routes
        # any path containing an undated edge into its second return value, so
        # a bare `pre_award` count measures "every edge on the path is dated"
        # rather than "a path was found" — passing cutoff=date.max does NOT
        # fix that, because the `d is not None` test still rejects the edge.
        # Both lists must be summed. (This bit me on the first run: 3/10
        # looked like a broken resolver when it was 3 MPs whose Commons
        # entries happen to carry dates.)
        dated, undated = find_paths(
            {p.id for p in person_candidates}, goal.id, adj, args.max_hops, date.max
        )
        any_paths = dated + undated
        pre_award, _ = find_paths({p.id for p in person_candidates}, goal.id, adj, args.max_hops)

        if any_paths:
            retrieved += 1
        if pre_award:
            retrieved_preaward += 1

        row["status"] = "retrieved" if any_paths else "not_retrieved"
        row["path_count"] = len(any_paths)
        row["preaward_path_count"] = len(pre_award)
        row["example_path"] = (
            [f"{e.edge_type}@{e.valid_from}" for e in any_paths[0]] if any_paths else []
        )
        results.append(row)

    n = len(results)
    print(f"\n=== POSITIVE CONTROLS (n={n}, max {args.max_hops} hops) ===")
    print(f"retrieved (any path)      : {retrieved}/{n}")
    print(f"retrieved (pre-award only): {retrieved_preaward}/{n}")
    unresolved = sum(1 for r in results if r["status"] == "unresolved")
    print(f"unresolved                : {unresolved}/{n}")
    print(
        "\nA low pre-award number here is a property of the SOURCE (the Lords\n"
        "register publishes no start dates), not evidence about relationships."
    )

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(
            {
                "n": n,
                "retrieved": retrieved,
                "retrieved_preaward": retrieved_preaward,
                "rows": results,
            },
            f,
            indent=2,
        )
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
