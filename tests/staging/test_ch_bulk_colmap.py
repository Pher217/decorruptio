"""Tests for Companies House bulk CSV column-name mapping.

Focuses on the ingestion of a single company row through
`ingest_ch_bulk_csv`, proving that some header spellings are accepted
and others are silently dropped today.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest

from uncorrupt.staging.companies_house import ingest_ch_bulk_csv
from uncorrupt.staging.models import Company


def _write_bulk_csv(tmp_path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> Path:
    csv_path = tmp_path / "bulk.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            full = dict.fromkeys(fieldnames, "")
            full.update(row)
            writer.writerow(full)
    return csv_path


@pytest.mark.django_db
class TestIncorporationDateHeaderSpelling:
    def test_incorporation_date_read_from_incorporationdate_header(self, tmp_path):
        """GIVEN a CH bulk CSV whose date column is spelled 'IncorporationDate'
        WHEN ingest_ch_bulk_csv() ingests one active company row
        THEN the company's incorporation_date is parsed as 2017-11-06."""
        csv_path = _write_bulk_csv(
            tmp_path,
            ["CompanyName", "CompanyNumber", "CompanyStatus", "IncorporationDate"],
            [
                {
                    "CompanyName": "ALPHA LTD",
                    "CompanyNumber": "00000001",
                    "CompanyStatus": "Active",
                    "IncorporationDate": "06/11/2017",
                }
            ],
        )

        ingest_ch_bulk_csv(csv_path)

        company = Company.objects.get(company_number="00000001")
        assert company.incorporation_date == date(2017, 11, 6)

    def test_incorporation_date_read_from_companyincorporationdate_header(self, tmp_path):
        """GIVEN a CH bulk CSV whose date column is spelled 'CompanyIncorporationDate'
        WHEN ingest_ch_bulk_csv() ingests one active company row
        THEN the company's incorporation_date is parsed as 2017-11-06."""
        csv_path = _write_bulk_csv(
            tmp_path,
            ["CompanyName", "CompanyNumber", "CompanyStatus", "CompanyIncorporationDate"],
            [
                {
                    "CompanyName": "ALPHA LTD",
                    "CompanyNumber": "00000001",
                    "CompanyStatus": "Active",
                    "CompanyIncorporationDate": "06/11/2017",
                }
            ],
        )

        ingest_ch_bulk_csv(csv_path)

        company = Company.objects.get(company_number="00000001")
        assert company.incorporation_date == date(2017, 11, 6)

    @pytest.mark.parametrize(
        "date_header,date_value",
        [
            ("IncorporationDate", "06/11/2017"),
            ("CompanyIncorporationDate", "06/11/2017"),
        ],
    )
    def test_company_name_and_status_survive_both_header_spellings(
        self, tmp_path, date_header, date_value
    ):
        """GIVEN a CH bulk CSV using either date header spelling
        WHEN ingest_ch_bulk_csv() ingests one active company row
        THEN the company_name and company_status are preserved exactly."""
        csv_path = _write_bulk_csv(
            tmp_path,
            ["CompanyName", "CompanyNumber", "CompanyStatus", date_header],
            [
                {
                    "CompanyName": "ALPHA LTD",
                    "CompanyNumber": "00000001",
                    "CompanyStatus": "Active",
                    date_header: date_value,
                }
            ],
        )

        ingest_ch_bulk_csv(csv_path)

        company = Company.objects.get(company_number="00000001")
        assert company.company_name == "ALPHA LTD"
        assert company.company_status == "Active"
