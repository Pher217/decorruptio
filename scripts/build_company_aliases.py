"""Build the general company-identifier alias table from the CH bulk CSV.

Regenerable, deterministic, no network access, no hand curation -- see
`uncorrupt.staging.aliases` for exactly what is (and is not) mapped and why.

Usage:
    PYTHONPATH=.:src python scripts/build_company_aliases.py \\
        --csv experiments/BasicCompanyDataAsOneFile-2026-07-01.csv \\
        --snapshot-date 2026-07-01 \\
        --out data/company_aliases.json

The output path defaults to `data/company_aliases.json`, inside this
repo's gitignored `data/` directory -- the artifact is a regenerable
derivative of the bulk CSV, not something to version alongside code (the
same convention already used for `experiments/`).
"""

from __future__ import annotations

import argparse
import os
from datetime import date
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.staging.aliases import build_alias_table, write_alias_table  # noqa: E402

DEFAULT_OUT = "data/company_aliases.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", required=True, help="Path to the CH bulk CSV snapshot")
    parser.add_argument(
        "--snapshot-date",
        required=True,
        type=date.fromisoformat,
        help="Snapshot date the CSV was downloaded (ISO, e.g. 2026-07-01)",
    )
    parser.add_argument("--out", default=DEFAULT_OUT)
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV not found: {csv_path}")

    aliases, report = build_alias_table(csv_path, args.snapshot_date)
    write_alias_table(aliases, args.out)

    print(f"rows scanned:                       {report.rows_scanned:,}")
    print(f"companies with >=1 former name:     {report.companies_with_any_former_name:,}")
    print(f"former-name cells seen:             {report.former_name_cells_seen:,}")
    print(f"candidate distinct former names:    {report.candidate_aliases:,}")
    print(f"dropped -- ambiguous among former:  {report.dropped_ambiguous_among_former_names:,}")
    print(f"dropped -- collides w/ current name:{report.dropped_collides_with_a_current_name:,}")
    print(f"aliases written:                    {report.aliases_written:,}")
    print(f"source:                             {report.source}")
    print(f"snapshot date:                      {report.snapshot_date}")
    print(f"output:                             {args.out}")


if __name__ == "__main__":
    main()
