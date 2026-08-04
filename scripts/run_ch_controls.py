"""Companies House-stratum external controls — retrieval + temporal.

Amendment v2.4 (§A2.4.2-3) requires per-stratum controls to be EXTERNALLY
SPECIFIED to gate a verdict: a control sampled from the graph (as
`run_positive_controls.py` deliberately is, and says so in its own
docstring) tests traversal conditional on the data already being ingested,
and is structurally blind to ingestion failure — the dominant failure mode
here. `uncorrupt.gates.stratum.measure_ch_officer_stratum` currently
reports `available=False` because no such fixture existed; this script and
`tests/fixtures/ch_temporal_controls.json` close that gap for Companies
House, mirroring `scripts/run_lords_controls.py`'s discipline.

Every one of the 12 controls in the fixture is a real (officer_id,
company_number, appointed_on) triple fetched live from the Companies House
REST API (never sampled from our own graph/DB — see the fixture's own
`source` block for the exact selection rule and retrieval method).

Unlike Lords (which publishes no interest start dates and can therefore
only ever be a RETRIEVAL control — see `run_lords_controls.py`), Companies
House DOES publish `appointed_on` for the overwhelming majority of officer
appointments (92.3% per the gate-measurement packet), so this script
reports two DISTINCT figures rather than one:

  retrieval -- does an `officer_of` edge exist at all between the resolved
               officer Entity and the resolved company Entity (any
               `valid_from`, or none)?
  temporal  -- among the `officer_of` edge(s) found, does at least one carry
               `valid_from` EXACTLY equal to the CH-verified `appointed_on`?

These are never conflated: a row can retrieve without its date matching (a
resolution succeeded but the ingested date is wrong, missing, or was
nulled by ch_appointments.py's inconsistent-dates guard), and the reverse
can never happen (a date match implies the edge, hence the relationship,
was retrieved).

Resolution is by exact registry identifier on both ends — the officer's CH
officer ID (`registry_scheme="GB-COH-OFFICER"`) and the company's CH number
(`registry_scheme="GB-COH"`) — never by fuzzy name matching. Unlike Lords
(where the register gives no queryable cross-register ID for a peer, so
weak surname matching is the only available test), Companies House hands
us a stable officer ID and company number directly; using anything weaker
here would dilute the one thing this control is built to measure — whether
a specific, externally verified appointment was actually ingested.

Selection is deterministic: the 12 controls are a fixed, pre-registered
list loaded from a JSON fixture in file order — nothing here samples,
shuffles, or reselects to flatter the result.

Usage:
    PYTHONPATH=.:src python scripts/run_ch_controls.py
    PYTHONPATH=.:src python scripts/run_ch_controls.py \\
        --controls tests/fixtures/ch_temporal_controls.json
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.graph.models import Edge, Entity  # noqa: E402
from uncorrupt.staging.companies_house import normalise_company_number  # noqa: E402

DEFAULT_CONTROLS_PATH = "tests/fixtures/ch_temporal_controls.json"
DEFAULT_OUT_PATH = "experiments/ch_controls.json"

STRATUM = "ch_officer_appointment"

STATUS_RECOVERED = "recovered"
STATUS_NOT_FOUND = "not-found"

TEMPORAL_MATCHED = "matched"
TEMPORAL_MISMATCH = "date_mismatch_or_missing"
TEMPORAL_NOT_APPLICABLE = "not_applicable"  # retrieval itself failed


def load_controls(path: str | Path = DEFAULT_CONTROLS_PATH) -> list[dict[str, Any]]:
    """Load the fixed control battery from its JSON fixture, in file order.

    No sampling, no seed, no reordering: the list returned is exactly the
    ``controls`` array as written in the fixture, every call, so the set
    cannot be reshuffled until it flatters a result.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(data["controls"])


def _resolve_officer(officer_id: str) -> Entity | None:
    return Entity.objects.filter(
        entity_type="person", registry_scheme="GB-COH-OFFICER", registry_id=officer_id
    ).first()


def _resolve_company(company_number: str) -> Entity | None:
    normalised = normalise_company_number(company_number)
    return Entity.objects.filter(
        entity_type="company", registry_scheme="GB-COH", registry_id=normalised
    ).first()


def classify_control(control: dict[str, Any]) -> dict[str, Any]:
    """Classify one control row: retrieval status + a distinct temporal status.

    Never conflates the two -- see the module docstring. `not-found` is
    used both for "officer/company absent from the graph" and "both
    resolved but no officer_of edge connects them"; this stratum has no
    fuzzy name matching, so there is no ambiguous-match ("unresolved")
    case to distinguish the way Lords does.
    """
    appointed_on = date.fromisoformat(control["appointed_on"])
    row: dict[str, Any] = {
        "id": control.get("id"),
        "query": control.get("query"),
        "officer_id": control["officer_id"],
        "officer_name": control.get("officer_name"),
        "company_number": control["company_number"],
        "company_name": control.get("company_name"),
        "appointed_on": control["appointed_on"],
    }

    officer = _resolve_officer(control["officer_id"])
    company = _resolve_company(control["company_number"])

    if officer is None or company is None:
        row["status"] = STATUS_NOT_FOUND
        row["reason"] = (
            "officer has no GB-COH-OFFICER entity in the graph"
            if officer is None
            else "company has no GB-COH entity in the graph"
        )
        row["temporal_status"] = TEMPORAL_NOT_APPLICABLE
        return row

    edges = list(
        Edge.objects.filter(edge_type="officer_of", source_entity=officer, target_entity=company)
    )
    if not edges:
        row["status"] = STATUS_NOT_FOUND
        row["reason"] = "officer and company both resolved but no officer_of edge connects them"
        row["temporal_status"] = TEMPORAL_NOT_APPLICABLE
        return row

    row["status"] = STATUS_RECOVERED
    row["edge_count"] = len(edges)
    row["edge_valid_from_values"] = [
        e.valid_from.isoformat() if e.valid_from else None for e in edges
    ]
    if any(e.valid_from == appointed_on for e in edges):
        row["temporal_status"] = TEMPORAL_MATCHED
    else:
        row["temporal_status"] = TEMPORAL_MISMATCH
    return row


def run_controls(controls_path: str | Path = DEFAULT_CONTROLS_PATH) -> dict[str, Any]:
    """Run the full control battery and return the summary + per-row results.

    Pure computation, no I/O beyond reading the fixture and querying the
    graph -- callers (CLI `main()`, tests) decide what to do with the result.
    """
    controls = load_controls(controls_path)
    rows = [classify_control(c) for c in controls]

    n = len(rows)
    retrieval_recovered = sum(1 for r in rows if r["status"] == STATUS_RECOVERED)
    not_found = sum(1 for r in rows if r["status"] == STATUS_NOT_FOUND)
    temporal_recovered = sum(1 for r in rows if r["temporal_status"] == TEMPORAL_MATCHED)

    return {
        "stratum": STRATUM,
        "n": n,
        "retrieval_recovered": retrieval_recovered,
        "retrieval_total": n,
        "temporal_recovered": temporal_recovered,
        "temporal_total": n,
        "not_found": not_found,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--controls", default=DEFAULT_CONTROLS_PATH)
    parser.add_argument("--out", default=DEFAULT_OUT_PATH)
    args = parser.parse_args()

    print(f"graph: {Entity.objects.count()} entities, {Edge.objects.count()} edges")

    result = run_controls(args.controls)

    print(f"\n=== COMPANIES HOUSE CONTROLS (n={result['n']}) ===")
    print(f"retrieval : {result['retrieval_recovered']}/{result['retrieval_total']}")
    print(f"temporal  : {result['temporal_recovered']}/{result['temporal_total']}")
    print(f"not-found : {result['not_found']}/{result['n']}")
    for row in result["rows"]:
        print(
            f"  [{row['status']:10s} temporal={row['temporal_status']:22s}] "
            f"{row['officer_name']!s:<35s} -> {row['company_name']}"
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
