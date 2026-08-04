"""Commons-stratum external controls — retrieval + temporal.

Amendment v2.4 (§A2.4.2-3) requires per-stratum controls to be EXTERNALLY
SPECIFIED to gate a verdict — see `scripts/run_ch_controls.py`'s module
docstring for the full rationale (a graph-sampled control like
`run_positive_controls.py` is structurally blind to ingestion failure).
`uncorrupt.gates.stratum.measure_commons_stratum` currently reports
`available=False` because no such fixture existed; this script and
`tests/fixtures/commons_retrieval_controls.json` close that gap for the
House of Commons, mirroring `scripts/run_lords_controls.py`'s discipline.

Every one of the 12 controls in the fixture is a real (member, declared
organisation, registrationDate) fact fetched live from
`https://interests-api.parliament.uk`, using the SAME counterparty-name
extraction the real ingest module
(`uncorrupt.graph.parliament_interests._extract_counterparty_name`) uses —
imported read-only, never modified — so a control's `organisation_name` is
exactly the string the pipeline itself would try to resolve. See the
fixture's own `source` block for the exact selection rule.

THE COVERAGE BOUNDARY (read before trusting any output of this script):
this source is severely under-ingested (~0.6%, 25 of 4,057 interests) as of
this control set's construction — see `uncorrupt.gates.coverage
.measure_commons_coverage`. Most rows below are EXPECTED to classify
`not-found`. That is the correct, informative result of running this
control battery against the current graph — it measures real coverage, and
tuning the fixture to avoid that result would defeat the entire point of
building an externally-specified control in the first place.

Two figures, never conflated (see `run_ch_controls.py` for why):

  retrieval -- does a `declared_interest` edge exist between the resolved
               member Entity and the resolved organisation Entity (any
               `valid_from`, or none)?
  temporal  -- among the `declared_interest` edge(s) found, does at least
               one carry `valid_from` EXACTLY equal to the externally
               verified `registration_date`?

Resolution: the member is resolved by exact Parliament member ID
(`registry_scheme="UK-PARLIAMENT-MEMBER"`) — Parliament hands us this ID
directly, so using anything weaker would dilute the ingestion-coverage
signal this control exists to measure. The organisation is resolved by
company number first (exact identifier match) when the control carries
one, falling back to exact normalised-name matching against
`entity_type="company"` Entities only — mirroring
`scripts.phase_c_paths.resolve_supplier`/`run_lords_controls
.resolve_company_candidates` — and deliberately never matching a
`UK-PARLIAMENT-UNRESOLVED` placeholder Entity, which is a weak,
per-interest-scoped node, not a verifiable registry record (same
discipline as Lords' `UK-LORDS-UNRESOLVED`).

A 2+ ambiguous name match is `unresolved`, distinct from `not-found` —
identical three-way split to `run_lords_controls.py`, for the same reason:
ambiguity is a resolution-LOGIC limitation the pipeline correctly refuses
to guess through (ADR-006), never a data-coverage gap.

Selection is deterministic: the 12 controls are a fixed, pre-registered
list loaded from a JSON fixture in file order.

Usage:
    PYTHONPATH=.:src python scripts/run_commons_controls.py
    PYTHONPATH=.:src python scripts/run_commons_controls.py \\
        --controls tests/fixtures/commons_retrieval_controls.json
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

from scripts.phase_c_paths import normalise_name, prefer_companies_house  # noqa: E402

from uncorrupt.graph.models import Edge, Entity  # noqa: E402
from uncorrupt.staging.companies_house import normalise_company_number  # noqa: E402

DEFAULT_CONTROLS_PATH = "tests/fixtures/commons_retrieval_controls.json"
DEFAULT_OUT_PATH = "experiments/commons_controls.json"

STRATUM = "commons_declared_interest"

STATUS_RECOVERED = "recovered"
STATUS_UNRESOLVED = "unresolved"
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


def _resolve_member(member_id: int | str) -> Entity | None:
    return Entity.objects.filter(
        entity_type="person", registry_scheme="UK-PARLIAMENT-MEMBER", registry_id=str(member_id)
    ).first()


def resolve_organisation_candidates(
    organisation_name: str, company_number: str | None
) -> list[Entity]:
    """Resolve a declared organisation to `entity_type="company"` Entities.

    Company number first (exact identifier), falling back to exact
    normalised-name matching -- never a `UK-PARLIAMENT-UNRESOLVED`
    placeholder, which is a per-interest scoped node, not a verifiable
    registry entity (mirrors `run_lords_controls.resolve_company_candidates`).
    """
    if company_number:
        normalised = normalise_company_number(company_number)
        by_number = list(
            Entity.objects.filter(
                entity_type="company", registry_scheme="GB-COH", registry_id=normalised
            )
        )
        if by_number:
            return by_number

    target = normalise_name(organisation_name)
    if not target:
        return []
    # Anchor the DB pre-filter on the first word of the NORMALISED name, not
    # a fixed-length slice of the raw declared name: a raw-name slice can
    # straddle a punctuation/suffix difference the real Companies House name
    # doesn't share -- e.g. "Guardian news a[nd media]" (control #1) vs the
    # real "Guardian News & Media Limited": the "&" breaks a same-position
    # substring match well before character 15, even though `normalise_name`
    # resolves both to the same string. The first normalised word is never
    # affected by a trailing suffix difference and, since `normalise_name`
    # already strips the leading/joining words this module treats as noise
    # ("The", "And", ...), is a safe icontains anchor.
    words = target.split()
    prefix = words[0] if words else organisation_name.strip()[:15]
    nearby = Entity.objects.filter(entity_type="company", name__icontains=prefix)[:200]
    return prefer_companies_house([e for e in nearby if normalise_name(e.name) == target])


def classify_control(control: dict[str, Any]) -> dict[str, Any]:
    """Classify one control row: recovered / unresolved / not-found, plus a
    distinct temporal status. Never conflates retrieval and temporal."""
    reg_date = date.fromisoformat(control["registration_date"])
    row: dict[str, Any] = {
        "id": control.get("id"),
        "interest_id": control.get("interest_id"),
        "member_id": control["member_id"],
        "member_name": control.get("member_name"),
        "organisation_name": control["organisation_name"],
        "company_number": control.get("company_number"),
        "registration_date": control["registration_date"],
    }

    member = _resolve_member(control["member_id"])
    org_candidates = resolve_organisation_candidates(
        control["organisation_name"], control.get("company_number")
    )
    row["organisation_candidates"] = len(org_candidates)

    if len(org_candidates) >= 2:
        row["status"] = STATUS_UNRESOLVED
        row["reason"] = "declared organisation name matched 2+ company nodes; not guessed"
        row["temporal_status"] = TEMPORAL_NOT_APPLICABLE
        return row

    if member is None or len(org_candidates) != 1:
        row["status"] = STATUS_NOT_FOUND
        row["reason"] = (
            "member has no UK-PARLIAMENT-MEMBER entity in the graph"
            if member is None
            else "declared organisation matched no company entity in the graph"
        )
        row["temporal_status"] = TEMPORAL_NOT_APPLICABLE
        return row

    org = org_candidates[0]
    edges = list(
        Edge.objects.filter(edge_type="declared_interest", source_entity=member, target_entity=org)
    )
    if not edges:
        row["status"] = STATUS_NOT_FOUND
        row["reason"] = (
            "member and organisation both resolved but no declared_interest edge connects them"
        )
        row["temporal_status"] = TEMPORAL_NOT_APPLICABLE
        return row

    row["status"] = STATUS_RECOVERED
    row["resolved_organisation"] = org.name
    row["edge_count"] = len(edges)
    row["edge_valid_from_values"] = [
        e.valid_from.isoformat() if e.valid_from else None for e in edges
    ]
    row["temporal_status"] = (
        TEMPORAL_MATCHED if any(e.valid_from == reg_date for e in edges) else TEMPORAL_MISMATCH
    )
    return row


def run_controls(controls_path: str | Path = DEFAULT_CONTROLS_PATH) -> dict[str, Any]:
    """Run the full control battery and return the summary + per-row results."""
    controls = load_controls(controls_path)
    rows = [classify_control(c) for c in controls]

    n = len(rows)
    retrieval_recovered = sum(1 for r in rows if r["status"] == STATUS_RECOVERED)
    unresolved = sum(1 for r in rows if r["status"] == STATUS_UNRESOLVED)
    not_found = sum(1 for r in rows if r["status"] == STATUS_NOT_FOUND)
    temporal_recovered = sum(1 for r in rows if r["temporal_status"] == TEMPORAL_MATCHED)

    return {
        "stratum": STRATUM,
        "n": n,
        "retrieval_recovered": retrieval_recovered,
        "retrieval_total": n,
        "temporal_recovered": temporal_recovered,
        "temporal_total": n,
        "unresolved": unresolved,
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

    print(f"\n=== COMMONS CONTROLS (n={result['n']}) ===")
    print(f"retrieval  : {result['retrieval_recovered']}/{result['retrieval_total']}")
    print(f"temporal   : {result['temporal_recovered']}/{result['temporal_total']}")
    print(f"unresolved : {result['unresolved']}/{result['n']}")
    print(f"not-found  : {result['not_found']}/{result['n']}")
    for row in result["rows"]:
        print(
            f"  [{row['status']:10s} temporal={row['temporal_status']:22s}] "
            f"{row['member_name']!s:<25s} -> {row['organisation_name']}"
        )
    print(
        "\nA low retrieval number here is expected -- the Commons source is severely\n"
        "under-ingested (see module docstring); this measures real coverage."
    )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
