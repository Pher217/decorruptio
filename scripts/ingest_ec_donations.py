"""Fetch + ingest Electoral Commission donation records (Phase 1.2).

Downloads the EC's own CSV export (via `/api/csv/Donations`, the same link
the search UI's "Export Results" button uses) for a date range, stores it
under `experiments/` with a provenance record, then ingests it into the
graph app's Entity/Edge tables.

Company-registration-number donors join to `staging.Company` with zero name
matching. Donors without a company number fall back to a uniqueness-guarded
exact name match (2+ candidates ⇒ no match, never a guess). Individual
donors are never turned into Entities (ADR-004 D1 — no private-individual
profiling); see `uncorrupt.graph.ec_donations` module docstring.

Usage:
    uv run python scripts/ingest_ec_donations.py --from 2019-01-01 --to 2022-12-31
"""

from __future__ import annotations

import argparse
import os
from datetime import date

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from uncorrupt.graph.ec_donations import fetch_ec_donations_csv, ingest_ec_donations_csv

DEFAULT_OUTPUT_DIR = "experiments/ec_donations"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--from", dest="from_date", type=date.fromisoformat, required=True)
    parser.add_argument("--to", dest="to_date", type=date.fromisoformat, required=True)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip the download and ingest an existing CSV in --output-dir.",
    )
    args = parser.parse_args()

    csv_path = f"{args.output_dir}/ec_donations.csv"
    if not args.skip_fetch:
        result = fetch_ec_donations_csv(args.from_date, args.to_date, args.output_dir)
        print(f"Fetched {result.row_count} rows -> {result.csv_path} ({result.content_hash})")
        csv_path = str(result.csv_path)

    summary = ingest_ec_donations_csv(csv_path)
    print(
        f"Ingested: {summary['matched']} donation edges, "
        f"{summary['unmatched_donor']} unmatched donors, "
        f"{summary['skipped_individual']} individual donors skipped "
        f"(of {summary['total']} rows)"
    )


if __name__ == "__main__":
    main()
