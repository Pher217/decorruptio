"""Link parliamentarians to their Companies House officer records.

Creates `same_as` edges so a path from a peer can reach the officer graph. See
`uncorrupt.graph.identity_resolution` -- this asserts identity, never merges.

Usage:
    PYTHONPATH=.:src python scripts/resolve_cross_register_identity.py --dry-run
    PYTHONPATH=.:src python scripts/resolve_cross_register_identity.py
"""

from __future__ import annotations

import argparse
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
    args = parser.parse_args()

    stats = resolve_cross_register_identities(dry_run=args.dry_run)
    for key, value in stats.items():
        print(f"{key:24s}: {value}")


if __name__ == "__main__":
    main()
