"""Fetch + ingest UK Register of Overseas Entities (ROE) records.

Enumerates registered-overseas-entity companies via the Companies House
advanced search (optionally bounded by an incorporation-date window — see
`uncorrupt.graph.overseas_entities` module docstring for why: the search
endpoint is offset-capped at 10,000 results per window), then fetches each
entity's profile + beneficial owners + managing officers, caching
(personal-data-filtered) responses under `experiments/` with a provenance
record, then ingests the cache into the graph app's Entity/Edge tables.

Requires a free API key exported as `COMPANIES_HOUSE_API_KEY` and a
`sources/uk_roe.yml` register entry (already present in this repo).

Usage:
    uv run python scripts/ingest_overseas_entities.py \
        --incorporated-from 2023-01-01 --incorporated-to 2023-01-31
    uv run python scripts/ingest_overseas_entities.py --company-number OE027594
"""

from __future__ import annotations

import argparse
import os
from datetime import date

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.graph.overseas_entities import (
    DEFAULT_MAX_CACHE_AGE_DAYS,
    fetch_overseas_entities,
    fetch_overseas_entity_details,
    ingest_overseas_entities,
)

DEFAULT_OUTPUT_DIR = "experiments/overseas_entities"


def _read_jsonl_company_numbers(path: str) -> list[str]:
    import json

    with open(path, encoding="utf-8") as f:
        return [json.loads(line)["company_number"] for line in f if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--incorporated-from", type=date.fromisoformat, default=None)
    parser.add_argument("--incorporated-to", type=date.fromisoformat, default=None)
    parser.add_argument(
        "--company-number",
        action="append",
        dest="company_numbers",
        help="A single OE company number (repeatable) — skips enumeration.",
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_CACHE_AGE_DAYS,
        help=f"Refetch a cached entity older than this many days (default "
        f"{DEFAULT_MAX_CACHE_AGE_DAYS}).",
    )
    args = parser.parse_args()

    if args.company_numbers:
        company_numbers = list(args.company_numbers)
    else:
        enum_result = fetch_overseas_entities(
            args.output_dir,
            incorporated_from=args.incorporated_from,
            incorporated_to=args.incorporated_to,
        )
        print(
            f"Enumerated {enum_result.company_count} entities "
            f"(hits={enum_result.hits}, truncated={enum_result.truncated}) "
            f"-> {enum_result.jsonl_path}",
            flush=True,
        )
        company_numbers = _read_jsonl_company_numbers(str(enum_result.jsonl_path))

    results = fetch_overseas_entity_details(
        company_numbers, args.output_dir, max_cache_age_days=args.max_age_days
    )
    fetched = sum(1 for r in results if not r.cached)
    cached = sum(1 for r in results if r.cached)
    print(f"Fetched {fetched} entity detail bundles ({cached} already cached)", flush=True)

    summary = ingest_overseas_entities(company_numbers, args.output_dir)
    print(
        f"Ingested: {summary['entities_created']} entities created, "
        f"{summary['entities_updated']} updated, "
        f"{summary['companies_unmatched']} unmatched (of {summary['total_companies']}), "
        f"{summary['corporate_bo_edges_created']} corporate beneficial-owner edges, "
        f"{summary['individual_bo_count']} individual beneficial owners "
        f"(count only, no identity), "
        f"{summary['corporate_managing_officer_edges_created']} corporate managing-officer "
        f"edges, "
        f"{summary['individual_managing_officer_count']} individual managing officers "
        f"(count only, no identity)"
    )


if __name__ == "__main__":
    main()
