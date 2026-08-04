"""Lords-stratum retrieval controls — the Lords analogue of run_positive_controls.py.

Amendment v2.4 requires per-stratum controls. Every other stratum had one; the
House of Lords did not, because `parliament.uk` returned 403 to every plain
fetch and was recorded as gating and unavailable. That 403 was a Cloudflare
bot-challenge, not an IP block — a real browser clears it — and a frozen,
hashed snapshot of the live register now exists (see
`tests/fixtures/lords_retrieval_controls.json` for the 12-row control battery
and its provenance). This script closes the control-battery gap: it takes
those 12 register-visible facts (peer name, declared company, exact register
wording, page number — all independently checkable against the snapshot) and
asks the pipeline to recover each peer<->company relationship **from the
graph alone**, using the same resolution and path-search code every other
control uses (`scripts.phase_c_paths`, `scripts.run_positive_controls`).

Selection is deterministic: the 12 controls are a fixed, pre-registered list
loaded from a JSON fixture in file order — nothing here samples, shuffles, or
reselects to flatter the result. They were deliberately spread across pages
1-35 of the (alphabetically paginated) register precisely so a front-loaded
surname bias could not sneak in.

THE BOUNDARY (read before trusting any output of this script): this is a
RETRIEVAL control, not a temporal one. The Lords register publishes no
interest start dates — `Edge.valid_from` is null by design for this source
(see `lords_interests.py`) — and the earliest Wayback capture of the register
(2020-06-17) postdates most award dates in scope, so pre-award evidence is
unavailable for the Lords stratum no matter how good retrieval is. This
script therefore reports **retrieval only** (a path exists in the graph, any
path, dated or not) and never computes or emits anything that could be read
as a temporal pass. Per v2.4, Lords supports the atemporal secondary
endpoint with validated retrieval; the strict temporal endpoint stays
INSTRUMENT-LIMITED for this stratum, full stop — `ENDPOINT` and
`TEMPORAL_ENDPOINT_STATUS` below are fixed constants, not computed from row
outcomes, so no row content can ever flip them.

Classification (three-way, not the two-way "unresolved"/"retrieved" split
`run_positive_controls.py` uses, because this control battery needs to tell
apart two very different failure modes):

  recovered  -- peer resolved, declared company resolved to exactly one
                `entity_type="company"` node, and a graph path connects them
                within `--max-hops`.
  unresolved -- the declared company name matched 2+ distinct company nodes
                and the pipeline correctly refuses to guess which one is
                right (ADR-006 discipline: ambiguity is never merged away).
                This is a genuine resolution-LOGIC limitation.
  not-found  -- the declared company matched zero `entity_type="company"`
                nodes, OR the peer has no person-entity in the graph at all,
                OR both resolved uniquely but no path connects them. This is
                a data-coverage gap (the register interest may still exist in
                the graph as an unresolved placeholder, e.g.
                `UK-LORDS-UNRESOLVED`, just not as a verifiable company
                node) -- NOT a retrieval-logic failure, and must never be
                counted as one.

Usage:
    PYTHONPATH=.:src python scripts/run_lords_controls.py
    PYTHONPATH=.:src python scripts/run_lords_controls.py --max-hops 2 \\
        --controls tests/fixtures/lords_retrieval_controls.json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

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
from scripts.run_positive_controls import as_published  # noqa: E402

from uncorrupt.graph.models import Edge, Entity  # noqa: E402

DEFAULT_CONTROLS_PATH = "tests/fixtures/lords_retrieval_controls.json"
DEFAULT_OUT_PATH = "experiments/lords_controls.json"

# Fixed, never computed from row data -- this script tests RETRIEVAL only.
# See the module docstring's "THE BOUNDARY" section. A row that resolves and
# finds a path is "recovered", never "temporal pass", and nothing below can
# turn that into a temporal claim.
ENDPOINT = "retrieval"
TEMPORAL_ENDPOINT_STATUS = "INSTRUMENT-LIMITED"

STATUS_RECOVERED = "recovered"
STATUS_UNRESOLVED = "unresolved"
STATUS_NOT_FOUND = "not-found"


def load_controls(path: str | Path = DEFAULT_CONTROLS_PATH) -> list[dict[str, Any]]:
    """Load the fixed control battery from its JSON fixture, in file order.

    No sampling, no seed, no reordering: the list returned is exactly the
    ``controls`` array as written in the fixture, every call, so the set
    cannot be reshuffled until it flatters a result.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(data["controls"])


def _people_by_surname() -> dict[str, list[Entity]]:
    """Index every person Entity by surname, once per run.

    Peer resolution then looks up `surname(as_published(peer_name))` against
    this index -- the same external-table-style matching
    `run_positive_controls.py` uses: titles and territorial designation
    stripped first, so this exercises the same weak matching every other
    control must survive rather than a string-equality check against the
    register's own form.
    """
    index: dict[str, list[Entity]] = defaultdict(list)
    for person in Entity.objects.filter(entity_type="person"):
        sn = surname(person.name)
        if sn:
            index[sn].append(person)
    return index


def resolve_company_candidates(declared_company: str) -> list[Entity]:
    """Resolve a declared company name to `entity_type="company"` Entities.

    Identical method to `run_positive_controls.py`: an `icontains` prefilter
    (postgres can't index a normalised expression cheaply here) narrowed to
    an exact `normalise_name` match, then GB-COH preferred over GLEIF-LEI on
    a tie (ADR-006). This deliberately does NOT match
    `UK-LORDS-UNRESOLVED`/`regulated_entity` placeholder nodes -- those are
    per-interest scoped placeholders, not verifiable company records, and
    treating them as a "company match" would let a weak (0.5-confidence,
    name-only) resolution masquerade as a retrieved company link.
    """
    target = normalise_name(declared_company)
    prefix = declared_company.strip()[:15]
    nearby = Entity.objects.filter(entity_type="company", name__icontains=prefix)[:200]
    return prefer_companies_house([e for e in nearby if normalise_name(e.name) == target])


def classify_control(
    control: dict[str, Any],
    people_by_surname: dict[str, list[Entity]],
    adj: dict[int, list[Any]],
    max_hops: int,
) -> dict[str, Any]:
    """Classify one control row as recovered / unresolved / not-found.

    Never computes or reports a temporal metric -- see the module docstring.
    """
    peer_name = control["peer_name"]
    declared_company = control["declared_company"]
    published_peer = as_published(peer_name)
    peer_candidates = people_by_surname.get(surname(published_peer), [])
    company_candidates = resolve_company_candidates(declared_company)

    row: dict[str, Any] = {
        "id": control.get("id"),
        "page": control.get("page"),
        "member_id": control.get("member_id"),
        "peer_name": peer_name,
        "peer_as_published": published_peer,
        "declared_company": declared_company,
        "peer_candidates": len(peer_candidates),
        "company_candidates": len(company_candidates),
    }

    if len(company_candidates) >= 2:
        row["status"] = STATUS_UNRESOLVED
        row["reason"] = "declared company name matched 2+ company nodes; not guessed"
        return row

    if not peer_candidates or len(company_candidates) != 1:
        row["status"] = STATUS_NOT_FOUND
        row["reason"] = (
            "peer has no person-entity in the graph"
            if not peer_candidates
            else "declared company matched no company-entity in the graph"
        )
        return row

    goal = company_candidates[0]
    # Retrieval = a path exists at all, dated or not. Both `find_paths`
    # return lists are summed deliberately -- a bare "dated-only" count would
    # be read as a temporal signal, which this stratum must never emit (see
    # the module docstring's boundary section).
    dated, undated = find_paths(
        {p.id for p in peer_candidates}, goal.id, adj, max_hops, cutoff=date.max
    )
    any_paths = dated + undated

    row["resolved_company"] = goal.name
    row["resolved_company_registry_scheme"] = goal.registry_scheme
    if any_paths:
        row["status"] = STATUS_RECOVERED
        row["path_count"] = len(any_paths)
        row["example_path"] = [f"{e.edge_type}" for e in any_paths[0]]
    else:
        row["status"] = STATUS_NOT_FOUND
        row["reason"] = "peer and company both resolved uniquely but no path connects them"
        row["path_count"] = 0

    return row


def run_controls(
    controls_path: str | Path = DEFAULT_CONTROLS_PATH,
    max_hops: int = 2,
) -> dict[str, Any]:
    """Run the full control battery and return the summary + per-row results.

    Pure computation, no I/O beyond reading the fixture and querying the
    graph -- callers (CLI `main()`, tests) decide what to do with the result.
    """
    controls = load_controls(controls_path)
    people_by_surname = _people_by_surname()
    adj = build_adjacency()

    rows = [classify_control(c, people_by_surname, adj, max_hops) for c in controls]

    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        counts[row["status"]] += 1

    return {
        "stratum": "lords",
        "endpoint": ENDPOINT,
        "temporal_endpoint_status": TEMPORAL_ENDPOINT_STATUS,
        "n": len(rows),
        "recovered": counts[STATUS_RECOVERED],
        "unresolved": counts[STATUS_UNRESOLVED],
        "not_found": counts[STATUS_NOT_FOUND],
        "max_hops": max_hops,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--controls", default=DEFAULT_CONTROLS_PATH)
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--out", default=DEFAULT_OUT_PATH)
    args = parser.parse_args()

    print(f"graph: {Entity.objects.count()} entities, {Edge.objects.count()} edges")

    result = run_controls(args.controls, args.max_hops)

    print(f"\n=== LORDS RETRIEVAL CONTROLS (n={result['n']}, max {args.max_hops} hops) ===")
    print(f"endpoint                 : {result['endpoint']}")
    print(f"temporal_endpoint_status : {result['temporal_endpoint_status']}")
    print(f"recovered                : {result['recovered']}/{result['n']}")
    print(f"unresolved (ambiguous)   : {result['unresolved']}/{result['n']}")
    print(f"not-found                : {result['not_found']}/{result['n']}")
    for row in result["rows"]:
        print(
            f"  [{row['status']:10s}] id={row['id']:<3} {row['peer_name']:<28s} "
            f"-> {row['declared_company']}"
        )
    print(
        "\nThis is a RETRIEVAL control only. The Lords register carries no "
        "interest start dates, so no row above is, or could ever be, a "
        "temporal (pre-award) pass -- see the module docstring."
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
