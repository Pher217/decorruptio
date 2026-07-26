"""Fetch + ingest GLEIF Legal Entity Identifier records.

Downloads a slice of the GLEIF public API (the same CC0 Golden Copy data
GLEIF publishes as a bulk download) for a given country, stores it under
`experiments/` with a provenance record, then ingests it into the graph
app's Entity table. GB records carrying a Companies House registration
number are cross-linked to `staging.Company` via
`uncorrupt.staging.companies_house.normalise_company_number`.

Usage:
    uv run python scripts/ingest_gleif.py --country GB --limit 50000
    uv run python scripts/ingest_gleif.py --limit 500  # global sample, no country filter
"""

from __future__ import annotations

import argparse
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.graph.gleif import fetch_gleif, ingest_gleif

DEFAULT_OUTPUT_DIR = "experiments/gleif"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--country", default=None, help="ISO 3166-1 alpha-2 country code (e.g. GB)")
    parser.add_argument("--limit", type=int, default=50_000)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip the download and ingest an existing JSONL file in --output-dir.",
    )
    args = parser.parse_args()

    suffix = args.country.lower() if args.country else "all"
    jsonl_path = f"{args.output_dir}/gleif_{suffix}.jsonl"
    if not args.skip_fetch:
        result = fetch_gleif(args.output_dir, country=args.country, limit=args.limit)
        print(
            f"Fetched {result.record_count} records -> {result.jsonl_path} ({result.content_hash})",
            flush=True,
        )
        jsonl_path = str(result.jsonl_path)

    summary = ingest_gleif(jsonl_path)
    print(
        f"Ingested: {summary['created']} created, {summary['updated']} updated, "
        f"{summary['skipped_no_lei']} skipped (no LEI), "
        f"{summary['gb_linked']} GB records linked to staging.Company "
        f"(Companies House RA codes only), "
        f"{summary['gb_other_authority']} GB records from a non-Companies-House "
        f"authority left unlinked (authority + number preserved in properties), "
        f"{summary['countries']} countries represented "
        f"(of {summary['total']} records)",
        flush=True,
    )


if __name__ == "__main__":
    main()
