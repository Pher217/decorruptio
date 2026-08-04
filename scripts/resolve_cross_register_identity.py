"""Link parliamentarians to their Companies House officer records.

Creates `same_as` edges so a path from a peer can reach the officer graph. See
`uncorrupt.graph.identity_resolution` -- this asserts identity, never merges.

Usage:
    PYTHONPATH=.:src python scripts/resolve_cross_register_identity.py --dry-run
    PYTHONPATH=.:src python scripts/resolve_cross_register_identity.py
"""

from __future__ import annotations

import argparse
import json
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.graph.identity_resolution import (  # noqa: E402
    resolve_cross_register_identities,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--out",
        default="experiments/resolve_cross_register_identity_undecidable.json",
        help="Where to write the full undecidable_members detail (a summary count "
        "prints inline; the per-member list is too large for a terminal line).",
    )
    args = parser.parse_args()

    stats = resolve_cross_register_identities(dry_run=args.dry_run)
    undecidable_members = stats.pop("undecidable_members")
    for key, value in stats.items():
        print(f"{key:32s}: {value}")
    print(f"{'undecidable_members':32s}: {len(undecidable_members)} (detail: {args.out})")

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(undecidable_members, f, indent=2)


if __name__ == "__main__":
    main()
