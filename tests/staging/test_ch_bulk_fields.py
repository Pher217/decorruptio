"""Tests for the Companies House bulk shell-signal backfill."""

from datetime import date

from scripts.backfill_ch_bulk_fields import (
    collect_sic,
    normalise_company_number,
    parse_ch_date,
)

from uncorrupt.staging.models import Company


def test_parse_ch_date_reads_the_ch_bulk_format():
    """GIVEN the dd/mm/yyyy format CH publishes in the bulk file
    WHEN the value is parsed
    THEN it returns the correct date, not a month/day transposition."""
    assert parse_ch_date("06/11/2017") == date(2017, 11, 6)


def test_parse_ch_date_accepts_iso():
    """GIVEN an ISO date (in case a future snapshot changes format)
    WHEN parsed
    THEN it is read correctly."""
    assert parse_ch_date("2017-11-06") == date(2017, 11, 6)


def test_parse_ch_date_empty_is_none():
    """GIVEN an empty or whitespace-only value
    WHEN parsed
    THEN it is None — never today's date."""
    assert parse_ch_date("") is None
    assert parse_ch_date("   ") is None
    assert parse_ch_date(None) is None


def test_parse_ch_date_malformed_is_none():
    """GIVEN an unparseable value
    WHEN parsed
    THEN it is None rather than a guess."""
    assert parse_ch_date("not-a-date") is None


def test_parse_ch_date_impossible_date_is_none():
    """GIVEN a syntactically valid but impossible date (month 13)
    WHEN parsed
    THEN it is None."""
    assert parse_ch_date("01/13/2023") is None


def test_collect_sic_drops_empty_columns():
    """GIVEN a row where only 2 of the 4 SIC columns carry a value
    WHEN SIC codes are collected
    THEN exactly those 2 are returned, stripped and in order."""
    row = {
        "SICCode.SicText_1": "64300 - Trusts and funds",
        "SICCode.SicText_2": "  ",
        "SICCode.SicText_3": " 64301 - Activities of investment trusts ",
        "SICCode.SicText_4": "",
    }
    assert collect_sic(row) == [
        "64300 - Trusts and funds",
        "64301 - Activities of investment trusts",
    ]


def test_collect_sic_no_codes_returns_empty_list():
    """GIVEN a row with no SIC values
    WHEN collected
    THEN the result is an empty list, not None."""
    assert collect_sic({"SICCode.SicText_1": ""}) == []


def test_backfill_join_key_is_zero_padded(db):
    """GIVEN the register stores '07015428' and the bulk CSV supplies '7015428'
    WHEN the CSV value is normalised into the join key
    THEN it matches the stored row — the padding bug that produced zero joins."""
    Company.objects.create(company_number="07015428", company_name="Test Co")
    assert normalise_company_number("7015428") == "07015428"
    assert Company.objects.filter(company_number=normalise_company_number("7015428")).exists()


def test_sic_codes_list_defaults_to_empty_list(db):
    """GIVEN a Company created without SIC codes
    WHEN it is read back
    THEN sic_codes_list is [] — the parsed field is separate from the raw
    text column, which stays TEXT because all 5.7M rows hold non-JSON strings."""
    company = Company.objects.create(company_number="00000001", company_name="X")
    company.refresh_from_db()
    assert company.sic_codes_list == []
