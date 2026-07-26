"""Fetch + ingest UK House of Lords Register of Interests (Phase 1.5).

Downloads the Lords register HTML pages (either live or from the Wayback
Machine), stores them under ``experiments/`` with a provenance record, then
ingests them into the graph app's Entity/Edge/Attestation tables.

The Lords register is an HTML page (no API). Each entry is free-text with
no individual registration dates — the Wayback snapshot date becomes
``Attestation.observed_at`` (transaction time). Counterparties with a
company number or unique exact name match join to ``staging.Company``.

Usage:
    # Live register (current state)
    uv run python scripts/ingest_lords_interests.py

    # Wayback snapshot (point-in-time, e.g. Nov 2020)
    uv run python scripts/ingest_lords_interests.py --wayback 20201130
"""

from __future__ import annotations

import argparse
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from uncorrupt.graph.lords_interests import (
    fetch_lords_register,
    ingest_lords_register,
)

DEFAULT_OUTPUT_DIR = "experiments/lords_interests"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--wayback",
        type=str,
        default=None,
        help="Wayback Machine timestamp (e.g. 20201130) for historical snapshot.",
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--max-pages",
        type=int,
        default=50,
        help="Maximum pages to fetch.",
    )
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip the download and ingest existing HTML in --output-dir.",
    )
    args = parser.parse_args()

    if not args.skip_fetch:
        result = fetch_lords_register(
            args.output_dir,
            wayback_timestamp=args.wayback,
            max_pages=args.max_pages,
        )
        print(
            f"Fetched {result.total_entries} entries across {result.page_count} pages "
            f"-> {result.html_path} ({result.content_hash})"
        )

    summary = ingest_lords_register(args.output_dir, wayback_timestamp=args.wayback)
    print(
        f"Ingested: {summary['matched']} declared_interest edges, "
        f"{summary['unmatched_counterparty']} unmatched counterparties, "
        f"{summary['skipped_private_individual']} private individuals skipped, "
        f"{summary['skipped_no_counterparty']} interests with no nameable counterparty, "
        f"{summary['nil_returns']} nil returns "
        f"(of {summary['total_interests']} interests across {summary['total_members']} members)"
    )


if __name__ == "__main__":
    main()
