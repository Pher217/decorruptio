"""Fetch + ingest Companies House officers records (Phase 1.4).

Fetches officers for a bounded list of company numbers from the CH REST
API (`/company/{company_number}/officers`), caching each company's
(personal-data-stripped) response under `experiments/` with a provenance
record, then ingests the cache into the graph app's Entity/Edge tables.

Requires a free API key exported as `COMPANIES_HOUSE_API_KEY`. See
`uncorrupt.graph.ch_officers` module docstring for scope boundaries
(company-officer capacity only; DOB/address/nationality dropped).

Usage:
    uv run python scripts/ingest_ch_officers.py --company-numbers-file numbers.txt
    uv run python scripts/ingest_ch_officers.py --company-number 12410514
"""

from __future__ import annotations

import argparse
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from uncorrupt.graph.ch_officers import (
    DEFAULT_MAX_CACHE_AGE_DAYS,
    fetch_company_officers,
    ingest_company_officers,
)

DEFAULT_OUTPUT_DIR = "experiments/ch_officers"


def _read_company_numbers(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--company-numbers-file",
        help="Path to a file with one Companies House company number per line.",
    )
    parser.add_argument(
        "--company-number",
        action="append",
        dest="company_numbers",
        help="A single company number (repeatable).",
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip the download and ingest an existing cache in --output-dir.",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_CACHE_AGE_DAYS,
        help="Refetch a cached company response older than this many days "
        f"(default {DEFAULT_MAX_CACHE_AGE_DAYS}).",
    )
    args = parser.parse_args()

    company_numbers = list(args.company_numbers or [])
    if args.company_numbers_file:
        company_numbers.extend(_read_company_numbers(args.company_numbers_file))
    if not company_numbers:
        parser.error("provide --company-number and/or --company-numbers-file")

    if not args.skip_fetch:
        results = fetch_company_officers(
            company_numbers, args.output_dir, max_cache_age_days=args.max_age_days
        )
        fetched = sum(1 for r in results if not r.cached)
        cached = sum(1 for r in results if r.cached)
        print(f"Fetched {fetched} companies ({cached} already cached) -> {args.output_dir}")

    summary = ingest_company_officers(company_numbers, args.output_dir)
    print(
        f"Ingested: {summary['edges_created']} officer edges "
        f"({summary['officers_no_id']} without a stable officer ID), "
        f"{summary['companies_unmatched']} companies unmatched "
        f"(of {summary['companies_processed']} processed, "
        f"{summary['total_officers']} officer records, "
        f"{summary['missing_appointed_on']} missing appointed_on)"
    )


if __name__ == "__main__":
    main()
