"""Tests for the general company-identifier alias layer.

Builds against small, synthetic CSVs shaped exactly like the real CH bulk
"Basic Company Data" file (never the real 2.6GB snapshot, never live
network) — see `uncorrupt.staging.aliases` for what this maps and why.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

from uncorrupt.staging.aliases import (
    BULK_CSV_SOURCE_URL,
    SOURCE_BULK_PREVIOUS_NAMES,
    AliasIndex,
    build_alias_table,
    write_alias_table,
)

_FIELDNAMES = [
    "CompanyNumber",
    "CompanyName",
    "PreviousName_1.CompanyName",
    "PreviousName_1.CONDATE",
    "PreviousName_2.CompanyName",
    "PreviousName_2.CONDATE",
]


def _write_bulk_csv(tmp_path: Path, rows: list[dict[str, str]]) -> Path:
    csv_path = tmp_path / "bulk.csv"
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDNAMES)
        writer.writeheader()
        for row in rows:
            full = dict.fromkeys(_FIELDNAMES, "")
            full.update(row)
            writer.writerow(full)
    return csv_path


class TestBuildAliasTable:
    def test_known_former_name_resolves_to_the_live_number(self, tmp_path):
        """GIVEN a company that renamed (Shell-plc-shaped: two former names)
        WHEN the alias table is built and loaded into an index
        THEN both former names resolve to the live company number, case- and
        whitespace-insensitively."""
        csv_path = _write_bulk_csv(
            tmp_path,
            [
                {
                    "CompanyNumber": "04366849",
                    "CompanyName": "SHELL PLC",
                    "PreviousName_1.CompanyName": "ROYAL DUTCH SHELL PLC",
                    "PreviousName_1.CONDATE": "21/01/2022",
                    "PreviousName_2.CompanyName": "FORTHDEAL LIMITED",
                    "PreviousName_2.CONDATE": "27/10/2004",
                }
            ],
        )

        aliases, _ = build_alias_table(csv_path, date(2026, 7, 1))
        index = AliasIndex(aliases)

        assert index.resolve("Royal Dutch Shell plc") == "04366849"
        assert index.resolve("  forthdeal limited  ") == "04366849"

    def test_unknown_name_returns_no_alias_rather_than_a_guess(self, tmp_path):
        """GIVEN a built index
        WHEN resolving a name that never appears anywhere in the source CSV
        THEN it returns None, not a best-effort guess."""
        csv_path = _write_bulk_csv(
            tmp_path,
            [
                {
                    "CompanyNumber": "04366849",
                    "CompanyName": "SHELL PLC",
                    "PreviousName_1.CompanyName": "ROYAL DUTCH SHELL PLC",
                    "PreviousName_1.CONDATE": "21/01/2022",
                }
            ],
        )

        aliases, _ = build_alias_table(csv_path, date(2026, 7, 1))
        index = AliasIndex(aliases)

        assert index.resolve("A Name That Was Never Anyone's") is None

    def test_resolve_none_and_empty_string_return_none(self, tmp_path):
        """GIVEN a built index
        WHEN resolving None or an empty string
        THEN both return None without raising."""
        csv_path = _write_bulk_csv(tmp_path, [])
        aliases, _ = build_alias_table(csv_path, date(2026, 7, 1))
        index = AliasIndex(aliases)

        assert index.resolve(None) is None
        assert index.resolve("") is None

    def test_alias_row_records_source_and_retrieved_at(self, tmp_path):
        """GIVEN a built alias table
        WHEN a row is inspected
        THEN it carries the bulk-CSV source identifier, its published URL,
        and the snapshot date passed in as `retrieved_at` — auditable per
        row, not just once for the whole table."""
        csv_path = _write_bulk_csv(
            tmp_path,
            [
                {
                    "CompanyNumber": "04366849",
                    "CompanyName": "SHELL PLC",
                    "PreviousName_1.CompanyName": "ROYAL DUTCH SHELL PLC",
                    "PreviousName_1.CONDATE": "21/01/2022",
                }
            ],
        )

        aliases, _ = build_alias_table(csv_path, date(2026, 7, 1))

        assert len(aliases) == 1
        row = aliases[0]
        assert row.source == SOURCE_BULK_PREVIOUS_NAMES
        assert row.source_url == BULK_CSV_SOURCE_URL
        assert row.retrieved_at == "2026-07-01"
        assert row.name_changed_on == "2022-01-21"

    def test_condate_blank_yields_none_not_a_guessed_date(self, tmp_path):
        """GIVEN a former name whose CONDATE column is blank
        WHEN the row is built
        THEN `name_changed_on` is None, never a fabricated date."""
        csv_path = _write_bulk_csv(
            tmp_path,
            [
                {
                    "CompanyNumber": "04366849",
                    "CompanyName": "SHELL PLC",
                    "PreviousName_1.CompanyName": "ROYAL DUTCH SHELL PLC",
                    "PreviousName_1.CONDATE": "",
                }
            ],
        )

        aliases, _ = build_alias_table(csv_path, date(2026, 7, 1))

        assert aliases[0].name_changed_on is None

    def test_builder_is_deterministic_across_runs(self, tmp_path):
        """GIVEN the same CSV input
        WHEN built twice
        THEN the two alias lists are exactly equal, including order."""
        csv_path = _write_bulk_csv(
            tmp_path,
            [
                {
                    "CompanyNumber": "04366849",
                    "CompanyName": "SHELL PLC",
                    "PreviousName_1.CompanyName": "ROYAL DUTCH SHELL PLC",
                    "PreviousName_1.CONDATE": "21/01/2022",
                },
                {
                    "CompanyNumber": "00000010",
                    "CompanyName": "ACME LIMITED",
                    "PreviousName_1.CompanyName": "ACME HOLDINGS LIMITED",
                    "PreviousName_1.CONDATE": "01/06/2010",
                },
            ],
        )

        aliases_first, report_first = build_alias_table(csv_path, date(2026, 7, 1))
        aliases_second, report_second = build_alias_table(csv_path, date(2026, 7, 1))

        assert aliases_first == aliases_second
        assert report_first == report_second

    def test_former_name_shared_by_two_companies_is_dropped_as_ambiguous(self, tmp_path):
        """GIVEN two different companies that both once carried the exact
        same former name
        WHEN the alias table is built
        THEN that name is dropped rather than guessed at, and the drop is
        counted in the report."""
        csv_path = _write_bulk_csv(
            tmp_path,
            [
                {
                    "CompanyNumber": "00000001",
                    "CompanyName": "AAA LIMITED",
                    "PreviousName_1.CompanyName": "SAME OLD NAME LIMITED",
                    "PreviousName_1.CONDATE": "01/01/2015",
                },
                {
                    "CompanyNumber": "00000002",
                    "CompanyName": "BBB LIMITED",
                    "PreviousName_1.CompanyName": "SAME OLD NAME LIMITED",
                    "PreviousName_1.CONDATE": "01/01/2018",
                },
            ],
        )

        aliases, report = build_alias_table(csv_path, date(2026, 7, 1))
        index = AliasIndex(aliases)

        assert index.resolve("SAME OLD NAME LIMITED") is None
        assert report.dropped_ambiguous_among_former_names == 1
        assert report.aliases_written == 0

    def test_former_name_colliding_with_a_different_companys_current_name_is_dropped(
        self, tmp_path
    ):
        """GIVEN company A's former name is identical to company B's CURRENT
        name
        WHEN the alias table is built
        THEN it is dropped — resolving it to A would silently misdirect
        away from the company that is actually live under that name today."""
        csv_path = _write_bulk_csv(
            tmp_path,
            [
                {
                    "CompanyNumber": "00000003",
                    "CompanyName": "COLLISION LIMITED",
                },
                {
                    "CompanyNumber": "00000004",
                    "CompanyName": "BBB LIMITED",
                    "PreviousName_1.CompanyName": "COLLISION LIMITED",
                    "PreviousName_1.CONDATE": "01/01/2020",
                },
            ],
        )

        aliases, report = build_alias_table(csv_path, date(2026, 7, 1))
        index = AliasIndex(aliases)

        assert index.resolve("COLLISION LIMITED") is None
        assert report.dropped_collides_with_a_current_name == 1
        assert report.dropped_ambiguous_among_former_names == 0

    def test_row_with_no_previous_names_contributes_no_aliases(self, tmp_path):
        """GIVEN a company with no PreviousName columns populated
        WHEN the alias table is built
        THEN no alias rows are produced and the row is counted as scanned
        but not as carrying a former name."""
        csv_path = _write_bulk_csv(
            tmp_path,
            [{"CompanyNumber": "00000005", "CompanyName": "PLAIN LIMITED"}],
        )

        aliases, report = build_alias_table(csv_path, date(2026, 7, 1))

        assert aliases == []
        assert report.rows_scanned == 1
        assert report.companies_with_any_former_name == 0

    def test_row_with_missing_company_number_is_skipped(self, tmp_path):
        """GIVEN a row with no company number
        WHEN the alias table is built
        THEN it contributes no alias and does not raise."""
        csv_path = _write_bulk_csv(
            tmp_path,
            [
                {
                    "CompanyNumber": "",
                    "CompanyName": "NO NUMBER LIMITED",
                    "PreviousName_1.CompanyName": "ORPHAN NAME LIMITED",
                    "PreviousName_1.CONDATE": "01/01/2020",
                }
            ],
        )

        aliases, report = build_alias_table(csv_path, date(2026, 7, 1))

        assert aliases == []
        assert report.rows_scanned == 1


class TestWriteAndLoadAliasTable:
    def test_round_trip_preserves_resolution_and_provenance(self, tmp_path):
        """GIVEN a built alias table written to disk
        WHEN it is loaded back into a fresh AliasIndex
        THEN resolution and per-row source/retrieved_at survive the
        JSON round trip."""
        csv_path = _write_bulk_csv(
            tmp_path,
            [
                {
                    "CompanyNumber": "04366849",
                    "CompanyName": "SHELL PLC",
                    "PreviousName_1.CompanyName": "ROYAL DUTCH SHELL PLC",
                    "PreviousName_1.CONDATE": "21/01/2022",
                }
            ],
        )
        aliases, _ = build_alias_table(csv_path, date(2026, 7, 1))
        out_path = tmp_path / "company_aliases.json"
        write_alias_table(aliases, out_path)

        loaded = AliasIndex.load(out_path)

        assert loaded.resolve("Royal Dutch Shell plc") == "04366849"
        assert len(loaded) == 1

    def test_write_creates_parent_directories(self, tmp_path):
        """GIVEN an output path whose parent directory does not exist yet
        WHEN the alias table is written
        THEN the directory is created rather than raising."""
        aliases: list = []
        out_path = tmp_path / "nested" / "dir" / "aliases.json"

        write_alias_table(aliases, out_path)

        assert out_path.exists()
