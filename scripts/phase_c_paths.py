"""Phase C — can the graph recover a VIP-lane supplier<->referrer relationship?

Phase C v1 asked only for a DIRECT edge between the referrer person and the
supplier company, and recovered 0 of 52. That is a necessary but very narrow
test: the relationships this cohort is about are rarely a single declared edge
between the two named parties. They are more often mediated -- a shared
directorship, a company both parties are attached to, a donation to a party the
referrer sits in.

This script widens the question to PATHS of length <= 2 while keeping every
discipline rule that made v1 credible:

  * The cohort is fixed. Rows come from `.consult/vip_lane_positives.csv`
    (the official DHSC High Priority Lane table). No row is added, dropped or
    reselected to improve the number.
  * Only PRE-AWARD evidence counts. An edge is admissible only if its
    `valid_from` is strictly before the award date. An edge with no
    `valid_from` cannot be shown to pre-date the award, so it is counted
    separately (`undated_only`) and never silently credited as a hit.
  * Resolution failures are reported, not hidden. A row whose supplier or
    referrer never resolved is `unresolved`, distinct from a resolved row with
    no path (`no_path`) -- conflating them would let poor matching masquerade
    as a negative finding.

Direction is ignored when walking (a relationship is symmetric for this
question even though the edge that records it is directed).

Usage:
    PYTHONPATH=.:src python scripts/phase_c_paths.py
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
from collections import defaultdict
from datetime import date

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.graph.models import Edge, Entity  # noqa: E402

COHORT_CSV = ".consult/vip_lane_positives.csv"
VIP_CH_CACHE = "experiments/vip_ch_cache.json"

# The lane operated through 2020; awards in the cohort are 2020 or later. Rows
# carry no machine-readable award date, so we use a single conservative cutoff
# rather than inventing a per-row date we cannot source.
AWARD_CUTOFF = date(2020, 3, 1)

_SUFFIXES = re.compile(
    r"\b(LIMITED|LTD|PLC|LLP|LP|GROUP|HOLDINGS|INTERNATIONAL|UK|THE|AND|CO)\b"
)


def normalise_company_number(cn: str) -> str:
    cn = (cn or "").strip().upper()
    m = re.match(r"^([A-Z]*)(\d+)$", cn)
    if not m:
        return cn
    prefix, digits = m.groups()
    return prefix + digits.zfill(8 - len(prefix))


def normalise_name(s: str) -> str:
    s = re.sub(r"[^A-Z0-9 ]", " ", (s or "").upper())
    s = _SUFFIXES.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def surname(person_name: str) -> str:
    """Last alphabetic token, lowercased.

    Peers are recorded in the register with titles ("Baroness Mone of
    Mayfair") and in the DHSC table without them, so a full-string match
    fails on almost every row. Surname is the one token both forms share.
    This is deliberately crude and OVER-matches; a hit found this way is a
    candidate, and the count it produces is a ceiling on true matches, not a
    claim about any individual.

    Peerage names carry a TERRITORIAL DESIGNATION after "of" -- "Lord Agnew of
    Oulton", "Baroness Mone of Mayfair". Taking the last token would yield
    "oulton" and "mayfair", which never match the "Agnew"/"Mone" an external
    table writes. Found by positive controls: 14 of 15 resolution failures
    were this.
    """
    name = re.split(r"\s+of\s+", person_name or "", maxsplit=1, flags=re.IGNORECASE)[0]
    tokens = [t for t in re.split(r"[^A-Za-z]+", name) if len(t) > 1]
    tokens = [
        t
        for t in tokens
        if t.lower() not in {"of", "the", "lord", "lady", "baroness", "baron", "sir", "dame"}
    ]
    return tokens[-1].lower() if tokens else ""


def resolve_supplier(name: str, ch_cache: dict) -> Entity | None:
    """Registry ID first, exact normalised name second. Never a fuzzy guess."""
    cached = ch_cache.get(name.strip())
    if cached and cached.get("company_number"):
        cn = normalise_company_number(cached["company_number"])
        found = Entity.objects.filter(company_number=cn).first()
        if found:
            return found
        return Entity.objects.filter(registry_scheme="GB-COH", registry_id=cn).first()

    target = normalise_name(name)
    if not target:
        return None
    nearby = Entity.objects.filter(
        entity_type="company", name__icontains=name.strip()[:15]
    )[:200]
    candidates = [e for e in nearby if normalise_name(e.name) == target]
    # Uniqueness guard: 2+ candidates means we cannot say which, so we say none.
    return candidates[0] if len(candidates) == 1 else None


def resolve_referrer(name: str, people_by_surname: dict) -> list[Entity]:
    sn = surname(name)
    return people_by_surname.get(sn, []) if sn else []


def build_adjacency() -> dict[int, list[Edge]]:
    adj: dict[int, list[Edge]] = defaultdict(list)
    for edge in Edge.objects.all().only(
        "id", "edge_type", "source_entity_id", "target_entity_id", "valid_from"
    ):
        adj[edge.source_entity_id].append(edge)
        adj[edge.target_entity_id].append(edge)
    return adj


def other_end(edge: Edge, entity_id: int) -> int:
    return (
        edge.target_entity_id
        if edge.source_entity_id == entity_id
        else edge.source_entity_id
    )


def find_paths(
    start_ids: set[int],
    goal_id: int,
    adj: dict[int, list[Edge]],
    max_hops: int,
    cutoff: date = AWARD_CUTOFF,
) -> tuple[list[list[Edge]], list[list[Edge]]]:
    """Return (pre_award_paths, undated_paths) up to `max_hops` edges.

    A path is pre-award only if EVERY edge on it has a `valid_from` strictly
    before the cutoff. A path with any undated edge is returned separately so
    it can be reported honestly rather than counted as a recovery.

    Note on why the split matters more than it looks: only 0.4% of
    `declared_interest` edges carry a `valid_from` at all (the Lords register
    publishes no start dates), against 92.3% of `officer_of`. A pre-award test
    is therefore unsatisfiable through register-of-interests data no matter how
    real the relationship is. Callers measuring *retrieval* rather than
    *temporal admissibility* should pass `cutoff=date.max`.
    """
    pre_award: list[list[Edge]] = []
    undated: list[list[Edge]] = []

    def walk(node: int, path: list[Edge], seen: set[int]) -> None:
        if len(path) >= max_hops:
            return
        for edge in adj.get(node, ()):
            nxt = other_end(edge, node)
            if nxt in seen:
                continue
            new_path = [*path, edge]
            if nxt == goal_id:
                dates = [e.valid_from for e in new_path]
                if all(d is not None and d < cutoff for d in dates):
                    pre_award.append(new_path)
                else:
                    undated.append(new_path)
                continue
            walk(nxt, new_path, seen | {nxt})

    for start in start_ids:
        walk(start, [], {start})
    return pre_award, undated


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--out", default="experiments/phase_c_paths.json")
    args = parser.parse_args()

    with open(VIP_CH_CACHE, encoding="utf-8") as f:
        ch_cache = json.load(f)

    people_by_surname: dict[str, list[Entity]] = defaultdict(list)
    for person in Entity.objects.filter(entity_type="person"):
        sn = surname(person.name)
        if sn:
            people_by_surname[sn].append(person)

    adj = build_adjacency()
    print(f"graph: {Entity.objects.count()} entities, {Edge.objects.count()} edges")
    print(f"people indexed by surname: {len(people_by_surname)} distinct surnames")

    rows, results = [], []
    with open(COHORT_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    counts = defaultdict(int)
    for row in rows:
        supplier_name = (row.get("supplier") or "").strip()
        referrer_name = (row.get("actual_referrer") or "").strip()
        counts["total"] += 1

        supplier = resolve_supplier(supplier_name, ch_cache)
        referrers = resolve_referrer(referrer_name, people_by_surname)
        if supplier:
            counts["supplier_resolved"] += 1
        if referrers:
            counts["referrer_resolved"] += 1
        if not supplier or not referrers:
            counts["unresolved"] += 1
            results.append(
                {
                    "supplier": supplier_name,
                    "referrer": referrer_name,
                    "status": "unresolved",
                    "supplier_resolved": bool(supplier),
                    "referrer_candidates": len(referrers),
                }
            )
            continue

        counts["both_resolved"] += 1
        pre_award, undated = find_paths(
            {r.id for r in referrers}, supplier.id, adj, args.max_hops
        )
        if pre_award:
            counts["path_found"] += 1
            status = "path_found"
        elif undated:
            counts["undated_only"] += 1
            status = "undated_only"
        else:
            counts["no_path"] += 1
            status = "no_path"

        results.append(
            {
                "supplier": supplier_name,
                "supplier_entity": supplier.name,
                "referrer": referrer_name,
                "referrer_candidates": len(referrers),
                "status": status,
                "pre_award_paths": [
                    [f"{e.edge_type}@{e.valid_from}" for e in p] for p in pre_award[:5]
                ],
                "undated_paths": [
                    [f"{e.edge_type}@{e.valid_from}" for e in p] for p in undated[:5]
                ],
            }
        )

    print(f"\n=== PHASE C (paths, max {args.max_hops} hops) ===")
    for key in (
        "total",
        "supplier_resolved",
        "referrer_resolved",
        "both_resolved",
        "unresolved",
        "path_found",
        "undated_only",
        "no_path",
    ):
        print(f"{key:20s}: {counts[key]}")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"counts": dict(counts), "rows": results}, f, indent=2)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
