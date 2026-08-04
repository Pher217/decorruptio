"""Electoral Commission-stratum external controls — retrieval + temporal.

Amendment v2.4 (§A2.4.2-3) requires per-stratum controls to be EXTERNALLY
SPECIFIED to gate a verdict — see `scripts/run_ch_controls.py`'s module
docstring for the full rationale. `uncorrupt.gates.stratum
.measure_electoral_commission_stratum` currently reports `available=False`
because no such fixture existed; this script and
`tests/fixtures/ec_retrieval_controls.json` close that gap, mirroring
`scripts/run_lords_controls.py`'s discipline.

NOT-IN-SCORER NOTE: `electoral_commission` is not one of
`run_gold_benchmark.MATERIAL_STRATA` (see
`uncorrupt.gates.stratum.donation_edges_are_ungated_in_scorer`) — a
passing/failing result here does not currently change the scorer's verdict
on its own. It still matters: it measures whether `donation` edges, which
CAN ride along on a mixed CONFIRMED/PARTIAL path today with their own
evidence completely unvalidated, are at least individually recoverable.

Every one of the 12 controls in the fixture is a real (donor company,
recipient, acceptedDate/receivedDate, donor company registration number)
fact fetched live from the same `/api/csv/Donations` export
`uncorrupt.graph.ec_donations.fetch_ec_donations_csv` uses — never sampled
from our own graph/DB. See the fixture's own `source` block for the exact
selection rule.

Two figures, never conflated (see `run_ch_controls.py` for why):

  retrieval -- does a `donation` edge exist between the resolved donor
               company Entity and the resolved recipient Entity (any
               `valid_from`, or none)?
  temporal  -- among the `donation` edge(s) found, does at least one carry
               `valid_from` equal to what `ec_donations.py`'s own ingest
               logic would compute for this row? That is `ReceivedDate`,
               falling back to `AcceptedDate` only when `ReceivedDate` is
               blank (see `ec_donations.py`'s module docstring) — NOT bare
               `AcceptedDate`, even though that is the externally
               human-facing "accepted" date. Comparing against the wrong
               field would report every correctly-ingested row as a
               temporal mismatch.

Resolution is by exact registry identifier on both ends: the donor by CH
company number (`registry_scheme="GB-COH"`), the recipient by its EC
`RegulatedEntityId` (`registry_scheme="EC-REGULATED-ENTITY"`) — both
identifiers the EC hands us directly, so using anything weaker would
dilute the ingestion-coverage signal this control exists to measure.

Selection is deterministic: the 12 controls are a fixed, pre-registered
list loaded from a JSON fixture in file order.

Usage:
    PYTHONPATH=.:src python scripts/run_ec_controls.py
    PYTHONPATH=.:src python scripts/run_ec_controls.py \\
        --controls tests/fixtures/ec_retrieval_controls.json
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

from uncorrupt.graph.ec_donations import _parse_ec_date, _recipient_entity_type  # noqa: E402
from uncorrupt.graph.models import Edge, Entity  # noqa: E402
from uncorrupt.staging.companies_house import normalise_company_number  # noqa: E402

DEFAULT_CONTROLS_PATH = "tests/fixtures/ec_retrieval_controls.json"
DEFAULT_OUT_PATH = "experiments/ec_controls.json"

STRATUM = "electoral_commission"

STATUS_RECOVERED = "recovered"
STATUS_NOT_FOUND = "not-found"

TEMPORAL_MATCHED = "matched"
TEMPORAL_MISMATCH = "date_mismatch_or_missing"
TEMPORAL_NOT_APPLICABLE = "not_applicable"


def load_controls(path: str | Path = DEFAULT_CONTROLS_PATH) -> list[dict[str, Any]]:
    """Load the fixed control battery from its JSON fixture, in file order.

    No sampling, no seed, no reordering.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return list(data["controls"])


def _resolve_donor(company_number: str) -> Entity | None:
    normalised = normalise_company_number(company_number)
    return Entity.objects.filter(
        entity_type="company", registry_scheme="GB-COH", registry_id=normalised
    ).first()


def _resolve_recipient(recipient_id: str | None, recipient_name: str) -> Entity | None:
    if recipient_id:
        return Entity.objects.filter(
            registry_scheme="EC-REGULATED-ENTITY", registry_id=recipient_id
        ).first()
    return Entity.objects.filter(
        registry_scheme="EC-REGULATED-ENTITY", registry_id__isnull=True, name=recipient_name
    ).first()


def _expected_valid_from(control: dict[str, Any]):
    """Mirrors `ec_donations.ingest_ec_donations_csv`'s own valid_from choice:
    ReceivedDate first, AcceptedDate only when ReceivedDate is blank."""
    received_raw = (control.get("received_date") or "").strip()
    if received_raw:
        return _parse_ec_date(received_raw)
    return _parse_ec_date(control.get("accepted_date") or "")


def classify_control(control: dict[str, Any]) -> dict[str, Any]:
    """Classify one control row: recovered / not-found, plus a distinct
    temporal status. Never conflates retrieval and temporal."""
    expected_valid_from = _expected_valid_from(control)
    row: dict[str, Any] = {
        "id": control.get("id"),
        "ec_ref": control.get("ec_ref"),
        "donor_name": control.get("donor_name"),
        "donor_company_number": control["donor_company_number"],
        "recipient_name": control["recipient_name"],
        "recipient_id": control.get("recipient_id"),
        "accepted_date": control.get("accepted_date"),
        "received_date": control.get("received_date"),
        "expected_valid_from": expected_valid_from.isoformat() if expected_valid_from else None,
    }

    donor = _resolve_donor(control["donor_company_number"])
    recipient_entity_type = _recipient_entity_type(control.get("recipient_type") or "")
    recipient = _resolve_recipient(control.get("recipient_id"), control["recipient_name"])
    row["recipient_entity_type_expected"] = recipient_entity_type

    if donor is None or recipient is None:
        row["status"] = STATUS_NOT_FOUND
        row["reason"] = (
            "donor has no GB-COH entity in the graph"
            if donor is None
            else "recipient has no EC-REGULATED-ENTITY entity in the graph"
        )
        row["temporal_status"] = TEMPORAL_NOT_APPLICABLE
        return row

    edges = list(
        Edge.objects.filter(edge_type="donation", source_entity=donor, target_entity=recipient)
    )
    if not edges:
        row["status"] = STATUS_NOT_FOUND
        row["reason"] = "donor and recipient both resolved but no donation edge connects them"
        row["temporal_status"] = TEMPORAL_NOT_APPLICABLE
        return row

    row["status"] = STATUS_RECOVERED
    row["edge_count"] = len(edges)
    row["edge_valid_from_values"] = [
        e.valid_from.isoformat() if e.valid_from else None for e in edges
    ]
    row["temporal_status"] = (
        TEMPORAL_MATCHED
        if expected_valid_from is not None
        and any(e.valid_from == expected_valid_from for e in edges)
        else TEMPORAL_MISMATCH
    )
    return row


def run_controls(controls_path: str | Path = DEFAULT_CONTROLS_PATH) -> dict[str, Any]:
    """Run the full control battery and return the summary + per-row results."""
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

    print(f"\n=== ELECTORAL COMMISSION CONTROLS (n={result['n']}) ===")
    print(f"retrieval : {result['retrieval_recovered']}/{result['retrieval_total']}")
    print(f"temporal  : {result['temporal_recovered']}/{result['temporal_total']}")
    print(f"not-found : {result['not_found']}/{result['n']}")
    for row in result["rows"]:
        print(
            f"  [{row['status']:10s} temporal={row['temporal_status']:22s}] "
            f"{row['donor_name']!s:<35s} -> {row['recipient_name']}"
        )
    print(
        "\nNOT one of run_gold_benchmark.MATERIAL_STRATA -- this result does not by\n"
        "itself change any scorer verdict (see module docstring)."
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
