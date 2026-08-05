"""Gate 1: does the s.9B (competition-law) disqualification seed have enough live
company footprint to justify building a person-anchored network at all?

CDDA 1986 s.9B is the CMA's competition-law disqualification power — the only section
that labels a person as cartel-implicated rather than insolvency-implicated (see
`scripts/disqualified_director_cross_register.py`, whose `CompaniesHouse` client,
`normalise_company_name`, and `ban_in_force` this script imports rather than
reimplements). That script corroborates hits through a person's *current officer
appointments*; this one asks a narrower question directly from the disqualification
record itself, without requiring the person to still be a listed officer anywhere:

    For each company named in an in-force s.9B disqualification's own `company_names`,
    is that company still alive today — either under the same name, or under a new
    one reached through a Companies House-asserted rename chain (the phoenix path)?

Resolution method: Companies House's `/search/companies` endpoint indexes previous
names, not just current ones (verified empirically — searching "CANTILLON LIMITED"
returns "MORRISROE DEMOLITION LIMITED", 00916538). But that search is fuzzy/token
matching, not an identity assertion: a query for "CANTILLON LIMITED" also surfaces
"CANTILLON CAPITAL LIMITED", an unrelated company. So a candidate is only accepted
here when the *candidate's own* `previous_company_names` (fetched from its profile)
contains the exact normalised name that the disqualification named — the same
CH-must-assert-the-link rule as ADR-004 D2, just applied to companies instead of
appointments.

Hard positive control: CANTILLON LIMITED (00916538) -> MORRISROE DEMOLITION LIMITED
is resolved directly, independent of seed sampling, on every run (see `control_check`
in the output). If that control fails, the resolution logic is broken and the rest of
the counts in this file must not be trusted — this script says so explicitly rather
than reporting them anyway.

Sampling note: `/search/disqualified-officers` requires a query term (an empty `q`
returns 0) and refuses `start_index` beyond ~5000, so the register cannot be
enumerated. Seeds here are a *convenience sample* by surname fragment, same
technique as `disqualified_director_cross_register.py`. s.9B disqualifications are a
genuinely small population (CMA director disqualification orders are rare relative to
the s.6/s.7 insolvency majority), so a generic-surname convenience sample is a weak
recall instrument for it specifically — `seed_count` here is a floor, not a
population estimate, and is reported as such.

Usage:
    PYTHONPATH=.:src uv run python scripts/measure_s9b_seed_footprint.py \\
        --seeds brown patel wilson ahmed murphy clark singh walsh khan davies \\
                evans roberts jones williams taylor thomas \\
        --per-seed 30 --output experiments/s9b_seed_footprint.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import urllib.parse
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any

from scripts.disqualified_director_cross_register import (
    COMPETITION_SECTION,
    CompaniesHouse,
    ban_in_force,
    normalise_company_name,
)

DEFAULT_OUTPUT = Path("experiments/s9b_seed_footprint.json")
DEFAULT_SEEDS = (
    "brown", "patel", "wilson", "ahmed", "murphy", "clark", "singh", "walsh",
    "khan", "davies", "evans", "roberts", "jones", "williams", "taylor", "thomas",
)
CONTROL_NAMED_COMPANY = "CANTILLON LIMITED"
CONTROL_EXPECTED_CURRENT_NAME = "MORRISROE DEMOLITION LIMITED"
CONTROL_EXPECTED_NUMBER = "00916538"
FORMATION_AGENT_THRESHOLD = 5  # companies sharing one registered office WITHIN this sample


@dataclass
class CompanyOutcome:
    named_company: str
    resolved_number: str | None
    resolved_current_name: str | None
    resolved_status: str | None
    outcome: str  # "active" | "successor" | "dissolved_or_unresolved"
    registered_office_address: str | None


COMPANY_NUMBER_PATTERN = re.compile(r"([A-Z0-9 &.,'/-]+?)\s*\((\d{6,8})\)")


def parse_named_companies(raw_entries: list[str]) -> list[tuple[str, str | None]]:
    """Split a disqualification's raw `company_names` entries into (name, number) pairs.

    Companies House sometimes packs MULTIPLE companies into a single list entry, each
    with its own company number in parentheses — observed live on the first run of
    this script: a single `company_names` element read "BROWN AND MASON GROUP LIMITED
    (01892133)  BROWN AND MASON LIMITED (00686405)" (case_identifier 50697, s.9B).
    Treating that whole string as one company name to search for resolves nothing,
    because it is not a real company name — it silently produced a false
    "dissolved_or_unresolved" even though 01892133 is active. When an entry embeds one
    or more "(NNNNNNN)" numbers, each is split out and resolved directly by number
    (deterministic — no fuzzy search needed). Only entries with no embedded number
    fall back to name-based fuzzy search via `resolve_company`.
    """
    pairs: list[tuple[str, str | None]] = []
    for entry in raw_entries:
        matches = COMPANY_NUMBER_PATTERN.findall(entry)
        if matches:
            pairs.extend((name.strip(), number) for name, number in matches)
        else:
            pairs.append((entry.strip(), None))
    return pairs


def resolve_company(
    client: CompaniesHouse, named_company: str, number: str | None = None
) -> dict[str, Any] | None:
    """Resolve a disqualification's named company to its live profile, or None.

    When `number` is known (parsed directly out of the raw `company_names` text by
    `parse_named_companies`), it is fetched directly — deterministic, no ambiguity.
    Otherwise falls back to `/search/companies`, accepting the first candidate whose
    OWN `previous_company_names` (or current name) contains the exact normalised named
    company — never the first fuzzy hit, since the search endpoint also returns
    unrelated companies with a similar name (e.g. "CANTILLON CAPITAL LIMITED" for a
    query of "CANTILLON LIMITED").
    """
    if number:
        profile = client.get(f"/company/{number}")
        return None if "_error" in profile else profile
    query = urllib.parse.urlencode({"q": named_company, "items_per_page": 15})
    result = client.get(f"/search/companies?{query}")
    target = normalise_company_name(named_company)
    for item in result.get("items") or []:
        number = item.get("company_number")
        if not number:
            continue
        profile = client.get(f"/company/{number}")
        if "_error" in profile:
            continue
        names = {normalise_company_name(profile.get("company_name"))}
        names |= {
            normalise_company_name(p.get("name"))
            for p in (profile.get("previous_company_names") or [])
        }
        if target in names:
            return profile
    return None


def classify(named_company: str, profile: dict[str, Any] | None) -> CompanyOutcome:
    """Turn a resolved (or unresolved) profile into one of the three footprint buckets."""
    if profile is None or profile.get("company_status") != "active":
        return CompanyOutcome(
            named_company=named_company,
            resolved_number=(profile or {}).get("company_number"),
            resolved_current_name=(profile or {}).get("company_name"),
            resolved_status=(profile or {}).get("company_status"),
            outcome="dissolved_or_unresolved",
            registered_office_address=None,
        )
    current_name = profile.get("company_name")
    address = profile.get("registered_office_address") or {}
    address_key = ", ".join(
        str(address.get(k, "")) for k in ("address_line_1", "locality", "postal_code")
    )
    outcome = "active" if normalise_company_name(current_name) == normalise_company_name(
        named_company
    ) else "successor"
    return CompanyOutcome(
        named_company=named_company,
        resolved_number=profile.get("company_number"),
        resolved_current_name=current_name,
        resolved_status="active",
        outcome=outcome,
        registered_office_address=address_key,
    )


def find_s9b_seeds(
    client: CompaniesHouse, seeds: tuple[str, ...], per_seed: int, today: date
) -> list[dict[str, Any]]:
    """Convenience-sample disqualified officers, keeping only in-force s.9B records."""
    people = client.sample_disqualified(seeds, per_seed)
    s9b_records = []
    for link in people:
        record = client.get(link)
        if "_error" in record:
            continue
        for disqualification in record.get("disqualifications") or []:
            if (disqualification.get("reason") or {}).get("section") != COMPETITION_SECTION:
                continue
            if not ban_in_force(disqualification, today):
                continue
            s9b_records.append({"record": record, "disqualification": disqualification})
            break
    return s9b_records


def run(client: CompaniesHouse, seeds: tuple[str, ...], per_seed: int, today: date) -> dict:
    control_profile = resolve_company(client, CONTROL_NAMED_COMPANY)
    control_outcome = classify(CONTROL_NAMED_COMPANY, control_profile)
    control_passed = (
        control_outcome.outcome == "successor"
        and control_outcome.resolved_number == CONTROL_EXPECTED_NUMBER
        and normalise_company_name(control_outcome.resolved_current_name)
        == normalise_company_name(CONTROL_EXPECTED_CURRENT_NAME)
    )

    s9b_seeds = find_s9b_seeds(client, seeds, per_seed, today)

    resolved_companies: dict[str, CompanyOutcome] = {}
    per_person: list[dict[str, Any]] = []
    for entry in s9b_seeds:
        record = entry["record"]
        disqualification = entry["disqualification"]
        raw_named = list(dict.fromkeys(disqualification.get("company_names") or []))
        named_pairs = parse_named_companies(raw_named)
        person_outcomes: list[CompanyOutcome] = []
        for name, number in named_pairs:
            dedupe_key = number or normalise_company_name(name)
            if dedupe_key not in resolved_companies:
                profile = resolve_company(client, name, number)
                resolved_companies[dedupe_key] = classify(name, profile)
            outcome = resolved_companies[dedupe_key]
            person_outcomes.append(outcome)
        per_person.append(
            {
                "person_number": record.get("person_number"),
                "surname": record.get("surname"),
                "forename": record.get("forename"),
                "disqualified_from": disqualification.get("disqualified_from"),
                "disqualified_until": disqualification.get("disqualified_until"),
                "named_companies_raw": raw_named,
                "named_companies_parsed": [f"{n} ({num})" if num else n for n, num in named_pairs],
                "outcomes": [o.outcome for o in person_outcomes],
                "dissolved_only": bool(person_outcomes)
                and all(o.outcome == "dissolved_or_unresolved" for o in person_outcomes),
            }
        )

    active = [o for o in resolved_companies.values() if o.outcome == "active"]
    successor = [o for o in resolved_companies.values() if o.outcome == "successor"]
    dissolved = [o for o in resolved_companies.values() if o.outcome == "dissolved_or_unresolved"]
    dissolved_only_seed_count = sum(1 for p in per_person if p["dissolved_only"])

    live = active + successor
    address_counts = Counter(
        o.registered_office_address for o in live if o.registered_office_address
    )
    suspect_addresses = {
        addr for addr, count in address_counts.items() if count >= FORMATION_AGENT_THRESHOLD
    }
    flagged = [o for o in live if o.registered_office_address in suspect_addresses]

    footprint_without_exclusion = len(live)
    footprint_with_exclusion = len(live) - len(flagged)

    return {
        "measured_on": today.isoformat(),
        "sampling": (
            "convenience sample by surname fragment against /search/disqualified-officers; "
            "NOT a population estimate. s.9B is a small population relative to the s.6/s.7 "
            "insolvency majority, so seed_count is a recall FLOOR, not a census figure."
        ),
        "seeds": list(seeds),
        "per_seed": per_seed,
        "control_check": {
            "named_company": CONTROL_NAMED_COMPANY,
            "expected_current_name": CONTROL_EXPECTED_CURRENT_NAME,
            "expected_number": CONTROL_EXPECTED_NUMBER,
            **asdict(control_outcome),
            "passed": control_passed,
        },
        "seed_count": len(s9b_seeds),
        "per_person": per_person,
        "active_companies": len(active),
        "active_companies_detail": [asdict(o) for o in active],
        "successor_companies": len(successor),
        "successor_companies_detail": [asdict(o) for o in successor],
        "dissolved_only_count": len(dissolved),
        "dissolved_only_seed_count": dissolved_only_seed_count,
        "formation_agent_threshold": FORMATION_AGENT_THRESHOLD,
        "formation_agent_note": (
            "Threshold applies WITHIN this sample's resolved companies only (no registry-wide "
            "address-frequency index was queried) — with a seed_count this small, this check "
            "cannot reliably detect true formation-agent mills and should be read as a weak "
            "signal, not a filter to trust."
        ),
        "formation_agent_flagged_count": len(flagged),
        "footprint_without_formation_agent_exclusion": footprint_without_exclusion,
        "footprint_with_formation_agent_exclusion": footprint_with_exclusion,
        "kill_threshold": "fewer than ~30-50 active-or-successor companies means the "
        "person-anchored network is not worth building",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seeds", nargs="+", default=list(DEFAULT_SEEDS))
    parser.add_argument("--per-seed", type=int, default=30)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    api_key = os.environ.get("COMPANIES_HOUSE_API_KEY")
    if not api_key:
        raise SystemExit("COMPANIES_HOUSE_API_KEY is not set")

    result = run(CompaniesHouse(api_key), tuple(args.seeds), args.per_seed, date.today())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2))

    print(f"control check (CANTILLON -> MORRISROE): passed={result['control_check']['passed']}")
    print(f"s.9B seed_count (convenience sample): {result['seed_count']}")
    print(f"active_companies: {result['active_companies']}")
    print(f"successor_companies: {result['successor_companies']}")
    print(f"dissolved_only_count (companies): {result['dissolved_only_count']}")
    print(f"dissolved_only_seed_count (people): {result['dissolved_only_seed_count']}")
    print(
        f"formation_agent_flagged_count (threshold={result['formation_agent_threshold']}): "
        f"{result['formation_agent_flagged_count']}"
    )
    print(f"footprint without exclusion: {result['footprint_without_formation_agent_exclusion']}")
    print(f"footprint with exclusion:    {result['footprint_with_formation_agent_exclusion']}")
    print(f"written to {args.output}")


if __name__ == "__main__":
    main()
