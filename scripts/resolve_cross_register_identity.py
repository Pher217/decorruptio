"""Link parliamentarians to their Companies House officer records.

Creates `same_as` edges so a path from a peer can reach the officer graph. See
`uncorrupt.graph.identity_resolution` -- this asserts identity, never merges.

This resolver is authoritative for `same_as`: it DELETES edges it no longer
proposes, so a run can destroy persisted data. It therefore defaults to a dry
run -- pass `--apply` to actually write.

Usage:
    PYTHONPATH=.:src python scripts/resolve_cross_register_identity.py
    PYTHONPATH=.:src python scripts/resolve_cross_register_identity.py --apply
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.graph.identity_resolution import (  # noqa: E402
    resolve_cross_register_identities,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write. Without this the run is a dry run: it reports the "
        "edges it would create and delete, and changes nothing.",
    )
    parser.add_argument(
        "--allow-bulk-delete",
        action="store_true",
        help="Permit deleting more than half the persisted same_as set. Refused "
        "by default, because that usually means an upstream ingest did not run "
        "rather than that the edges are genuinely stale.",
    )
    parser.add_argument(
        "--out",
        default="experiments/resolve_cross_register_identity_undecidable.json",
        help="Where to write the full undecidable_members detail (a summary count "
        "prints inline; the per-member list is too large for a terminal line).",
    )
    args = parser.parse_args()

    # Create the output directory BEFORE the run. `experiments/` is gitignored,
    # so on a fresh clone this open() raised FileNotFoundError -- previously
    # *after* the destructive delete had already committed, losing the report
    # of what had just happened.
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)

    stats = resolve_cross_register_identities(
        dry_run=not args.apply, allow_bulk_delete=args.allow_bulk_delete
    )
    undecidable_members = stats.pop("undecidable_members")
    print("MODE: " + ("APPLY (writing)" if args.apply else "DRY RUN (no changes)"))
    for key, value in stats.items():
        print(f"{key:32s}: {value}")
    print(f"{'undecidable_members':32s}: {len(undecidable_members)} (detail: {args.out})")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(undecidable_members, f, indent=2)


if __name__ == "__main__":
    main()
