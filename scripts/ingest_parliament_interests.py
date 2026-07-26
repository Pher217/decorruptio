"""Fetch + ingest UK Parliament register of interests records (Phase 1.3).

Downloads the Parliament Interests API's own JSON (`/api/v1/Interests`, with
`ExpandChildInterests=true` so payment-level entries arrive nested under
their parent), stores it under `experiments/` with a provenance record, then
ingests it into the graph app's Entity/Edge tables.

Counterparties with a company number or a unique exact company-name match
join to `staging.Company`. A 2+ candidate exact-name match is never guessed
(no edge, counted separately). Family-member categories and private
individuals are never turned into Entities/Edges — see
`uncorrupt.graph.parliament_interests` module docstring.

Usage:
    uv run python scripts/ingest_parliament_interests.py \\
        --registered-from 2026-01-01 --registered-to 2026-07-13
"""

from __future__ import annotations

import argparse
import os
from datetime import date

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from uncorrupt.graph.parliament_interests import (
    fetch_parliament_interests,
    ingest_parliament_interests_json,
    list_registers,
)

DEFAULT_OUTPUT_DIR = "experiments/parliament_interests"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--registered-from", dest="registered_from", type=date.fromisoformat, default=None
    )
    parser.add_argument(
        "--registered-to", dest="registered_to", type=date.fromisoformat, default=None
    )
    parser.add_argument(
        "--register-id",
        dest="register_id",
        type=int,
        default=None,
        help="Pin one specific published register document (see --list-registers). "
        "Note: registers only go back to 2024-03-18 — use --registered-from/--registered-to "
        "to reach older data.",
    )
    parser.add_argument(
        "--list-registers",
        action="store_true",
        help="Print available register documents (id, publishedDate) and exit.",
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip the download and ingest an existing JSON dump in --output-dir.",
    )
    args = parser.parse_args()

    if args.list_registers:
        for register in list_registers():
            print(f"{register.register_id}\t{register.published_date}\t{register.house_type}")
        return

    json_path = f"{args.output_dir}/parliament_interests.json"
    if not args.skip_fetch:
        result = fetch_parliament_interests(
            args.output_dir,
            registered_from=args.registered_from,
            registered_to=args.registered_to,
            register_id=args.register_id,
        )
        print(f"Fetched {result.item_count} items -> {result.json_path} ({result.content_hash})")
        json_path = str(result.json_path)

    summary = ingest_parliament_interests_json(json_path)
    print(
        f"Ingested: {summary['matched']} declared_interest edges, "
        f"{summary['unmatched_counterparty']} unmatched counterparties, "
        f"{summary['skipped_family']} family-member interests skipped, "
        f"{summary['skipped_private_individual']} private individuals skipped, "
        f"{summary['skipped_unclassified_counterparty']} unclassified counterparties skipped, "
        f"{summary['skipped_no_counterparty']} interests with no nameable counterparty, "
        f"{summary['inverted_interval']} inverted intervals guarded "
        f"(of {summary['total']} interests)"
    )


if __name__ == "__main__":
    main()
