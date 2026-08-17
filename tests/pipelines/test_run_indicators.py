"""Tests for scripts/run_indicators.py -- the flag-persistence entry point.

Fixture design: one Tender + one Award engineered so that exactly ONE
indicator (i006_incorporation_proximity) fires exactly one flag against
locale "gb", and every other VALIDATED-for-gb indicator (i001, i002, i003,
i004, i005, i007, i008) is starved of the condition it needs:

- i001 (single bidder, uk_contracts_finder branch): procurement_method has no
  "open"/"competitive"/"restricted" keyword -> skipped.
- i002 (short bid window): tender_start/tender_end are both null -> excluded
  from the queryset entirely.
- i003 (repeat-winner share): only 1 award for the buyer, MIN_BUYER_AWARDS=4
  -> below the denominator floor.
- i004 (price vs estimate): tender.value_amount_cents=0 -> excluded by the
  `tender_ref__value_amount_cents__gt=0` filter.
- i005 (direct-award share): only 1 tender for the buyer, MIN_BUYER_AWARDS=4
  -> below the denominator floor.
- i007 (value vs company size): accounts_category="full accounts" -- not in
  DORMANT/MICRO/SMALL categories -> no flag_reason.
- i008 (dormancy delinquency): accounts_category not dormant and
  accounts_last_made_up_date is null -> no flag_reason.
- i009 (contract splitting): UNVALIDATED for gb -> never runs at all.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
import scripts.run_indicators as run_indicators
from django.utils import timezone

from uncorrupt.staging.companies_house import resolve_suppliers
from uncorrupt.staging.models import Award, Company, Flag, SupplierResolution, Tender

SOURCE = "uk_contracts_finder"
LOCALE = "gb"


def _build_fixture() -> None:
    award_date = timezone.now() - timedelta(days=5)
    incorporation_date = award_date.date() - timedelta(days=10)

    tender = Tender.objects.create(
        source_id=SOURCE,
        tender_id="T1",
        title="Widget supply",
        procurement_method="single_tender_action",
        procurement_method_details="direct award",
        currency="GBP",
        value_amount_cents=0,
        buyer_name="Test Council",
        source_url="https://example.com/t1",
    )
    Award.objects.create(
        source_id=SOURCE,
        tender_id=tender.tender_id,
        award_id="A1",
        tender_ref=tender,
        supplier_name="Shell Co Ltd",
        supplier_id_scheme="GB-COH",
        supplier_id="12345678",
        currency="GBP",
        value_amount_cents=100_000,
        status="active",
        award_date=award_date,
    )
    company = Company.objects.create(
        company_number="12345678",
        company_name="Shell Co Ltd",
        incorporation_date=incorporation_date,
        accounts_category="full accounts",
        accounts_last_made_up_date=None,
    )
    SupplierResolution.objects.create(
        source_id=SOURCE,
        supplier_name="Shell Co Ltd",
        supplier_id_scheme="GB-COH",
        supplier_id="12345678",
        company=company,
        company_number="12345678",
        match_confidence=1.0,
        match_method="identifier",
    )
    # D1 (ADR-012) made resolution identifier-primary and derived: i006-i009 read
    # award.resolution and fail-closed when a source has no AwardResolution rows.
    # Derive them through the production path rather than hand-building the row.
    resolve_suppliers(SOURCE)


@pytest.mark.django_db
class TestRunIndicatorsPersistence:
    def test_persists_exactly_the_expected_number_of_flag_rows(self) -> None:
        """GIVEN a fixture engineered to produce exactly 1 flag (i006 only)
        WHEN scripts/run_indicators.run() is called for uk_contracts_finder/gb
        THEN Flag.objects.count() was 0 before and is exactly 1 after, and the
        one row is i006_incorporation_proximity.
        """
        _build_fixture()
        assert Flag.objects.count() == 0

        report = run_indicators.run(SOURCE, LOCALE)

        assert Flag.objects.count() == 1
        row = Flag.objects.get()
        assert row.indicator_id == "i006_incorporation_proximity"
        assert report["totals"]["persisted"] == 1

    def test_i009_does_not_run_for_locale_gb(self) -> None:
        """GIVEN locale gb, where i009_contract_splitting is UNVALIDATED
        WHEN run() selects indicators via enabled_for(locale)
        THEN i009 is reported skipped (not ran), and no persisted Flag row
        ever carries indicator_id == "i009_contract_splitting".
        """
        _build_fixture()

        report = run_indicators.run(SOURCE, LOCALE)

        assert "i009_contract_splitting" not in report["ran"]
        assert "i009_contract_splitting" in report["skipped_unvalidated"]
        assert not Flag.objects.filter(indicator_id="i009_contract_splitting").exists()

    def test_running_twice_does_not_duplicate_rows(self) -> None:
        """GIVEN a fixture that produces 1 flag
        WHEN run() is called twice in a row
        THEN Flag.objects.count() is still 1, not 2 -- the scoped delete makes
        the run idempotent.
        """
        _build_fixture()

        run_indicators.run(SOURCE, LOCALE)
        first_count = Flag.objects.count()
        run_indicators.run(SOURCE, LOCALE)
        second_count = Flag.objects.count()

        assert first_count == 1
        assert second_count == 1

    def test_dry_run_persists_nothing_but_still_reports_the_flag_count(self) -> None:
        """GIVEN --dry-run (dry_run=True)
        WHEN run() evaluates the fixture
        THEN zero Flag rows are persisted but the report's total flag count is
        still the non-zero count that would have been persisted.
        """
        _build_fixture()

        report = run_indicators.run(SOURCE, LOCALE, dry_run=True)

        assert Flag.objects.count() == 0
        assert report["totals"]["flags"] == 1
        assert report["totals"]["persisted"] == 0

    def test_json_export_flag_counts_match_persisted_row_counts_per_indicator(
        self, tmp_path
    ) -> None:
        """GIVEN a completed (non-dry-run) run written to --output
        WHEN the JSON report and the database are both inspected
        THEN each indicator's reported "persisted" count in the JSON export
        equals Flag.objects.filter(indicator_id=...).count() in the database.
        """
        _build_fixture()
        output_path = tmp_path / "report.json"

        exit_code = run_indicators.main(
            ["--source", SOURCE, "--locale", LOCALE, "--output", str(output_path)]
        )

        assert exit_code == 0
        report = json.loads(output_path.read_text())
        for indicator_id, stats in report["per_indicator"].items():
            db_count = Flag.objects.filter(indicator_id=indicator_id).count()
            assert stats["persisted"] == db_count, (
                f"{indicator_id}: report says {stats['persisted']}, db has {db_count}"
            )


@pytest.mark.django_db(transaction=True)
class TestIntegrityCheckRollsBack:
    def test_count_mismatch_raises_and_leaves_no_rows_behind(self, monkeypatch) -> None:
        """GIVEN a run where MORE rows reach the database than the report claims
        WHEN run() performs its persisted-row integrity check
        THEN it raises AssertionError AND the transaction is rolled back, so zero
        Flag rows survive.

        The divergence is injected as a DUPLICATE insert rather than a dropped row:
        dropping the only row would leave the database empty regardless of whether
        the rollback fired, making the assertion vacuously true. Inserting an extra
        row means a surviving row can only mean the rollback did NOT happen.
        """
        _build_fixture()
        real_bulk_create = Flag.objects.bulk_create

        def duplicating_bulk_create(objs, *args, **kwargs):
            objs = list(objs)
            return real_bulk_create(objs + objs, *args, **kwargs) if objs else []

        monkeypatch.setattr(Flag.objects, "bulk_create", duplicating_bulk_create)

        assert Flag.objects.count() == 0
        with pytest.raises(AssertionError, match="persisted-row count mismatch"):
            run_indicators.run(SOURCE, "gb")

        # Load-bearing: any surviving row proves the check fired after the commit.
        assert Flag.objects.count() == 0
