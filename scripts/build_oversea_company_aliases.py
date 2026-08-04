"""Build the oversea-company branch cross-reference alias table.

The general remediation for the gap `uncorrupt.staging.aliases` (former
company names) documented but could not close: NF/FC/SF-prefixed identifiers
are not renames, they are Companies House's oversea-company branch-
registration scheme, and their cross-reference to a live UK company number
(`foreign_company_details.registration_number`) is not in the bulk CSV --
it needs one live API call per branch registration. See
`uncorrupt.staging.oversea_company_aliases` for the full mechanism and its
typed, counted, never-silently-dropped outcomes.

Requires `COMPANIES_HOUSE_API_KEY` (free — see
https://developer.company-information.service.gov.uk/) and a populated
`staging.Company` table (the CH bulk CSV already ingested).

Usage:
    # Measure the universe first -- no network calls, no API key needed:
    PYTHONPATH=.:src python scripts/build_oversea_company_aliases.py --measure-only

    # Fetch + build (resumable; safe to re-run, skips already-cached identifiers):
    PYTHONPATH=.:src python scripts/build_oversea_company_aliases.py

    # Bound a partial/resumable run (re-invoke with the same --output-dir to continue):
    PYTHONPATH=.:src python scripts/build_oversea_company_aliases.py --limit 500

    # Rebuild the alias table from an already-fetched cache, no new network calls:
    PYTHONPATH=.:src python scripts/build_oversea_company_aliases.py --skip-fetch

The output path defaults to `data/oversea_company_aliases.json`, inside this
repo's gitignored `data/` directory -- a regenerable derivative of the live
register, not something to version alongside code (same convention as
`build_company_aliases.py`'s `data/company_aliases.json`).
"""

from __future__ import annotations

import argparse
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.staging.aliases import write_alias_table  # noqa: E402
from uncorrupt.staging.oversea_company_aliases import (  # noqa: E402
    OVERSEA_COMPANY_PREFIXES,
    build_oversea_company_alias_table,
    fetch_oversea_company_cross_references,
    oversea_company_legacy_numbers,
)

DEFAULT_OUTPUT_DIR = "data/ch_oversea_company_cache"
DEFAULT_OUT = "data/oversea_company_aliases.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument(
        "--measure-only",
        action="store_true",
        help="Print the per-prefix universe size and exit -- no network calls.",
    )
    args = parser.parse_args()

    legacy_numbers = oversea_company_legacy_numbers()
    by_prefix: dict[str, int] = dict.fromkeys(OVERSEA_COMPANY_PREFIXES, 0)
    for number in legacy_numbers:
        by_prefix[number[:2]] = by_prefix.get(number[:2], 0) + 1

    print(f"NF/FC/SF legacy identifiers in staging.Company: {len(legacy_numbers):,}")
    for prefix in OVERSEA_COMPANY_PREFIXES:
        print(f"  {prefix}: {by_prefix[prefix]:,}")

    if args.measure_only:
        return

    if args.limit:
        legacy_numbers = legacy_numbers[: args.limit]

    if not args.skip_fetch:
        counts = fetch_oversea_company_cross_references(legacy_numbers, args.output_dir)
        print(
            f"fetched {counts['fetched']:,}, cached {counts['cached']:,}, "
            f"not_found {counts['not_found']:,}, failed {counts['failed']:,}"
        )

    aliases, report = build_oversea_company_alias_table(legacy_numbers, args.output_dir)
    write_alias_table(aliases, args.out)

    print(f"legacy ids considered:        {report.legacy_ids_considered:,}")
    print(f"resolved:                     {report.resolved:,}")
    print(f"no cross-reference:           {report.no_cross_reference:,}")
    print(f"not oversea-company type:     {report.not_oversea_company:,}")
    print(f"not found (404):              {report.not_found:,}")
    print(f"self-referential/degenerate:  {report.self_referential:,}")
    print(f"never fetched:                {report.unfetched:,}")
    print(f"aliases written:              {report.aliases_written:,}")
    print(f"source:                       {report.source}")
    print(f"output:                       {args.out}")


if __name__ == "__main__":
    main()
