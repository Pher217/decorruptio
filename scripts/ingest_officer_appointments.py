"""Expand each known CH officer to their other appointments (the 2nd hop).

Without this, shared directorships are invisible: `ingest_ch_officers.py`
gives us the officers OF a company, but a referrer and a supplier connected
through a person who sits on both boards only becomes a path once that
person's *other* appointments are known.

Only officers already recorded as `GB-COH-OFFICER` entities are expanded --
this discovers no new people. See `uncorrupt.graph.ch_appointments`.

Usage:
    PYTHONPATH=.:src python scripts/ingest_officer_appointments.py
    PYTHONPATH=.:src python scripts/ingest_officer_appointments.py --limit 200
"""

from __future__ import annotations

import argparse
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.graph.ch_appointments import (  # noqa: E402
    fetch_officer_appointments,
    ingest_officer_appointments,
)
from uncorrupt.graph.models import Entity  # noqa: E402

DEFAULT_OUTPUT_DIR = "experiments/ch_appointments"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--skip-fetch", action="store_true")
    args = parser.parse_args()

    officer_ids = list(
        Entity.objects.filter(
            entity_type="person", registry_scheme="GB-COH-OFFICER"
        ).values_list("registry_id", flat=True)
    )
    if args.limit:
        officer_ids = officer_ids[: args.limit]
    print(f"officers to expand: {len(officer_ids)}")

    if not args.skip_fetch:
        counts = fetch_officer_appointments(officer_ids, args.output_dir)
        print(
            f"fetched {counts['fetched']}, cached {counts['cached']}, "
            f"failed {counts['failed']}"
        )

    stats = ingest_officer_appointments(officer_ids, args.output_dir)
    print(
        f"Ingested: {stats['edges_created']} officer_of edges from "
        f"{stats['officers_processed']} officers "
        f"({stats['appointments_seen']} appointments seen, "
        f"{stats['company_unmatched']} unmatched companies, "
        f"{stats['officer_missing']} officers not in graph, "
        f"{stats['inconsistent_dates']} with inconsistent source dates)"
    )


if __name__ == "__main__":
    main()
