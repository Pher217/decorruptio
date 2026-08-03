"""Backfill Wayback-archived Lords register snapshots as Attestation evidence.

Only 0.4% of `declared_interest` edges carry a `valid_from` — the Lords
register publishes no start dates. This script fetches historical editions
of the register from the Internet Archive and records, for every
already-known-or-newly-seen relationship, evidence that it was ALREADY on
the record as of that edition's date — promoting eligible rows from
ATEMPORAL_CORROBORATION (level 3) to PRE_AWARD_OBSERVED (level 2) on the
evidence ladder (see `uncorrupt.graph.register_snapshots`).

This does not touch `Edge.valid_from` (still null — we do not know the true
registration date) and does not touch the live-register ingest
(`lords_interests.ingest_lords_register`) — it only ADDS Attestation
evidence, keyed per-snapshot so repeated runs across different capture dates
accumulate rather than collapse onto one record.

Coverage is NOT uniform: page 1 of the register (roughly the first ~20
members, alphabetically) has ~50 unique Wayback captures from 2020-06
onward; deeper pages (checked live: page 2 ~14, page 5 ~7, page 20 ~4, page
40 ~6 captures over the same 6-year span) are archived far more sparsely.
A member on a late page may have no capture at all near a given target date
— report this as an archive coverage gap (`no_capture_available`), never as
evidence the relationship didn't exist then.

Usage:
    PYTHONPATH=.:src python scripts/ingest_register_snapshots.py \\
        --target-date 2020-03-01 --output-dir experiments/register_snapshots

    # Ingest every capture strictly before the target date, not just the
    # nearest one (more evidence, more HTTP calls):
    PYTHONPATH=.:src python scripts/ingest_register_snapshots.py \\
        --target-date 2020-03-01 --all-before
"""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.graph.lords_interests import LORDS_REGISTER_URL  # noqa: E402
from uncorrupt.graph.register_snapshots import (  # noqa: E402
    fetch_lords_snapshot,
    ingest_lords_snapshot,
    nearest_capture_before,
    query_wayback_cdx,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-date",
        required=True,
        help="ISO date (e.g. an award date). The nearest capture strictly "
        "before this date is ingested by default.",
    )
    parser.add_argument("--from-date", default=None, help="Wayback timestamp prefix, e.g. '2019'")
    parser.add_argument("--to-date", default=None, help="Wayback timestamp prefix, e.g. '2021'")
    parser.add_argument("--output-dir", default="experiments/register_snapshots")
    parser.add_argument(
        "--all-before",
        action="store_true",
        help="Ingest every capture strictly before --target-date, not just the nearest one.",
    )
    args = parser.parse_args()

    target = date.fromisoformat(args.target_date)

    print(f"querying Wayback CDX for {LORDS_REGISTER_URL} ...")
    captures = query_wayback_cdx(LORDS_REGISTER_URL, from_date=args.from_date, to_date=args.to_date)
    print(f"CDX: {len(captures)} unique captures found")

    if not captures:
        print("no Wayback captures available for this URL — archive coverage gap, not evidence")
        return

    if args.all_before:
        selected = sorted(
            (c for c in captures if c.captured_at.date() < target), key=lambda c: c.captured_at
        )
    else:
        nearest = nearest_capture_before(captures, target)
        selected = [nearest] if nearest else []

    if not selected:
        print(f"no capture found strictly before {target} — archive coverage gap")
        return

    print(f"ingesting {len(selected)} capture(s): {[c.timestamp for c in selected]}")

    for capture in selected:
        out_dir = Path(args.output_dir) / capture.timestamp
        fetch_result = fetch_lords_snapshot(capture, out_dir)
        summary = ingest_lords_snapshot(out_dir, capture, fetch_result.content_hash)
        print(f"\n=== capture {capture.timestamp} ({capture.captured_at.date()}) ===")
        for key, value in summary.items():
            print(f"{key:25s}: {value}")


if __name__ == "__main__":
    main()
