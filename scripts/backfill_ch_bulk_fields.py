"""Backfill shell-company signal fields onto staging.Company from the CH bulk CSV.

The bulk snapshot was loaded with only a few columns kept. The fields that
actually discriminate a shell from a trading company -- incorporation date
(company age at award), accounts category (dormant / micro-entity / total
exemption), status, and SIC codes -- were already on disk and simply unread.
Known gap this closes: Ayanda Capital (GBP252.5m of PPE contracts, incorporated
2017) is invisible to every current indicator.

No network access; reads `experiments/BasicCompanyDataAsOneFile-*.csv`.

Efficiency note: this does NOT look each row up before writing. It constructs
`Company` instances carrying only the primary key plus the updated fields and
hands them to `bulk_update`, so a row whose company_number is not in the table
simply matches nothing. A per-row `Company.objects.get()` would issue 5.7M
individual SELECTs.

Usage:
    PYTHONPATH=.:src python scripts/backfill_ch_bulk_fields.py
    PYTHONPATH=.:src python scripts/backfill_ch_bulk_fields.py --limit 50000
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from datetime import date, datetime
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.staging.companies_house import normalise_company_number  # noqa: E402
from uncorrupt.staging.models import Company  # noqa: E402

DEFAULT_CSV = "experiments/BasicCompanyDataAsOneFile-2026-07-01.csv"
BATCH_SIZE = 5000

_UPDATE_FIELDS = [
    "incorporation_date",
    "company_status",
    "company_category",
    "accounts_category",
    "accounts_next_due",
    "dissolution_date",
    "sic_codes_list",
]

_SIC_COLUMNS = (
    "SICCode.SicText_1",
    "SICCode.SicText_2",
    "SICCode.SicText_3",
    "SICCode.SicText_4",
)

# CH publishes dates as dd/mm/yyyy in this file. ISO is accepted too in case a
# future snapshot changes format. An unparseable value becomes None -- never
# today's date, never a guess.
_DATE_FORMATS = ("%d/%m/%Y", "%Y-%m-%d")


def parse_ch_date(value: str | None) -> date | None:
    value = (value or "").strip()
    if not value:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def collect_sic(row: dict[str, str]) -> list[str]:
    return [v.strip() for c in _SIC_COLUMNS if (v := row.get(c)) and v.strip()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--csv", default=DEFAULT_CSV)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    csv_path = Path(args.csv)
    if not csv_path.exists():
        sys.exit(f"CSV not found: {csv_path}")

    rows_read = 0
    rows_skipped = 0
    rows_updated = 0
    batch: list[Company] = []

    with open(csv_path, encoding="utf-8", errors="replace", newline="") as f:
        reader = csv.DictReader(f)
        # The CH header contains leading spaces on several columns
        # (" CompanyNumber", " PreviousName_1.CONDATE", ...). Strip them or
        # every lookup by clean name silently returns None.
        if reader.fieldnames:
            reader.fieldnames = [h.strip() for h in reader.fieldnames]

        for row in reader:
            rows_read += 1
            number = normalise_company_number(row.get("CompanyNumber") or "")
            if not number:
                rows_skipped += 1
                continue

            batch.append(
                Company(
                    company_number=number,
                    incorporation_date=parse_ch_date(row.get("IncorporationDate")),
                    company_status=(row.get("CompanyStatus") or "").strip() or None,
                    company_category=(row.get("CompanyCategory") or "").strip() or None,
                    accounts_category=(row.get("Accounts.AccountCategory") or "").strip() or None,
                    accounts_next_due=parse_ch_date(row.get("Accounts.NextDueDate")),
                    dissolution_date=parse_ch_date(row.get("DissolutionDate")),
                    sic_codes_list=collect_sic(row),
                )
            )

            if len(batch) >= BATCH_SIZE:
                rows_updated += Company.objects.bulk_update(batch, _UPDATE_FIELDS) or 0
                batch.clear()

            if rows_read % 100_000 == 0:
                print(f"  {rows_read:,} read / {rows_updated:,} updated", flush=True)

            if args.limit and rows_read >= args.limit:
                break

    if batch:
        rows_updated += Company.objects.bulk_update(batch, _UPDATE_FIELDS) or 0

    print(
        f"\nrows read: {rows_read:,}\n"
        f"rows skipped (no company number): {rows_skipped:,}\n"
        f"rows updated: {rows_updated:,}\n"
        f"rows not in staging.Company: {rows_read - rows_skipped - rows_updated:,}"
    )


if __name__ == "__main__":
    main()
