"""Tests for i009 contract_splitting — the buyer-side splitting indicator.

Per Opus spec (consult-claude.sh 2026-08-11):
- clear positive: 3 awards, same supplier+buyer, ≤7d, each < threshold, sum > threshold
- clear negative: framework/DPS call-offs (excluded)
- clear negative: only 2 awards (min_pieces=3)
- clear negative: same tender_id (multi-lot = one competed buy)
- clear negative: sum under threshold
"""

from __future__ import annotations

from datetime import date

import pytest

from uncorrupt.indicators.catalog.i009_contract_splitting import ContractSplitting
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.register.models import LocaleProfile
from uncorrupt.staging.companies_house import resolve_suppliers
from uncorrupt.staging.models import Award, Company, Tender

SOURCE = "uk_contracts_finder"
THRESHOLD_GBP = 122976
THRESHOLD_CENTS = THRESHOLD_GBP * 100


def _make_ctx() -> EvaluationContext:
    locale = LocaleProfile(
        code="gb",
        procedure_metadata={"open_tender_threshold_gbp": THRESHOLD_GBP},
    )
    return EvaluationContext(locale=locale, source_id=SOURCE)


def _setup_company(company_number: str = "12345678"):
    Company.objects.create(
        company_number=company_number,
        company_name="TEST SUPPLIER LTD",
        company_status="Active",
        incorporation_date=date(2020, 1, 1),
        accounts_category="FULL",
    )


def _setup_tender(tender_id: str, buyer: str = "Test Council", title: str = "Test procurement"):
    return Tender.objects.create(
        source_id=SOURCE,
        tender_id=tender_id,
        ocid=f"ocds-test-{tender_id}",
        title=title,
        status="active",
        procurement_method="open",
        currency="GBP",
        buyer_name=buyer,
        buyer_country="GB",
        raw_json={},
    )


def _setup_award(tender, award_id: str, supplier_name: str, value_cents: int, award_date_str: str):
    return Award.objects.create(
        source_id=SOURCE,
        tender_id=tender.tender_id,
        tender_ref=tender,
        award_id=award_id,
        supplier_name=supplier_name,
        supplier_id_scheme="GB-COH",
        supplier_id="12345678",
        currency="GBP",
        value_amount_cents=value_cents,
        status="active",
        award_date=f"{award_date_str}T00:00:00Z",
        raw_json={},
    )


class TestI009ContractSplitting:
    """i009: buyer splits one procurement into sub-threshold pieces."""

    @pytest.mark.django_db
    def test_clear_positive_3_awards_same_week(self):
        """GIVEN 3 awards from 3 distinct tenders to the same supplier+buyer
        within 7 days, each under the open-tender threshold, summing over it
        WHEN i009 evaluates THEN it flags the cluster."""
        _setup_company()
        supplier = "TEST SUPPLIER LTD"

        for i, (tid, d, val) in enumerate(
            [
                ("T1", "2025-01-06", 4000000),  # £40,000 — under threshold
                ("T2", "2025-01-08", 3500000),  # £35,000 — under threshold
                ("T3", "2025-01-10", 5000000),  # £50,000 — under threshold
            ]
        ):
            t = _setup_tender(tid)
            _setup_award(t, f"A{i + 1}", supplier, val, d)

        resolve_suppliers(SOURCE)
        ind = ContractSplitting()
        flags = list(ind.evaluate(_make_ctx()))

        assert len(flags) == 1, f"expected 1 flag, got {len(flags)}"
        f = flags[0]
        assert "3 contracts" in f.explanation
        assert "same-week" in f.explanation
        assert "125,000.00" in f.explanation  # sum = £125,000 > threshold
        assert len(f.evidence) == 3  # G6: one ProvenanceRecord per award

    @pytest.mark.django_db
    def test_negative_framework_calloff_excluded(self):
        """GIVEN 3 awards that are framework call-offs WHEN i009 evaluates
        THEN it flags nothing (framework/DPS is a legitimate single-supplier route)."""
        _setup_company()
        supplier = "TEST SUPPLIER LTD"

        for i, (tid, d, val) in enumerate(
            [
                ("T1", "2025-01-06", 4000000),
                ("T2", "2025-01-07", 3500000),
                ("T3", "2025-01-08", 5000000),
            ]
        ):
            t = _setup_tender(tid, title="Framework call-off for services")
            _setup_award(t, f"A{i + 1}", supplier, val, d)

        resolve_suppliers(SOURCE)
        ind = ContractSplitting()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 0

    @pytest.mark.django_db
    def test_negative_only_2_awards(self):
        """GIVEN only 2 awards (min_pieces=3) WHEN i009 evaluates THEN no flag."""
        _setup_company()
        supplier = "TEST SUPPLIER LTD"

        for i, (tid, d, val) in enumerate(
            [
                ("T1", "2025-01-06", 6000000),
                ("T2", "2025-01-07", 7000000),
            ]
        ):
            t = _setup_tender(tid)
            _setup_award(t, f"A{i + 1}", supplier, val, d)

        resolve_suppliers(SOURCE)
        ind = ContractSplitting()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 0

    @pytest.mark.django_db
    def test_negative_same_tender_id_multi_lot(self):
        """GIVEN 3 awards from the SAME tender_id (multi-lot = one competed buy)
        WHEN i009 evaluates THEN no flag (collapses to 1 piece < min_pieces)."""
        _setup_company()
        supplier = "TEST SUPPLIER LTD"

        t = _setup_tender("T1")
        for i, d, val in [
            (1, "2025-01-06", 4000000),
            (2, "2025-01-07", 3500000),
            (3, "2025-01-08", 5000000),
        ]:
            _setup_award(t, f"A{i}", supplier, val, d)

        resolve_suppliers(SOURCE)
        ind = ContractSplitting()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 0

    @pytest.mark.django_db
    def test_negative_sum_under_threshold(self):
        """GIVEN 3 awards each under threshold but sum also under threshold
        WHEN i009 evaluates THEN no flag."""
        _setup_company()
        supplier = "TEST SUPPLIER LTD"

        for i, (tid, d, val) in enumerate(
            [
                ("T1", "2025-01-06", 1000000),  # £10,000
                ("T2", "2025-01-07", 1000000),  # £10,000
                ("T3", "2025-01-08", 1000000),  # £10,000 — sum £30k < £122,976
            ]
        ):
            t = _setup_tender(tid)
            _setup_award(t, f"A{i + 1}", supplier, val, d)

        resolve_suppliers(SOURCE)
        ind = ContractSplitting()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 0

    @pytest.mark.django_db
    def test_abstain_when_no_threshold_in_locale(self):
        """GIVEN a locale without open_tender_threshold_gbp WHEN i009 evaluates
        THEN it abstains (units_evaluated=0, no flags)."""
        locale = LocaleProfile(code="gb", procedure_metadata={})
        ctx = EvaluationContext(locale=locale, source_id=SOURCE)
        ind = ContractSplitting()
        flags = list(ind.evaluate(ctx))
        assert len(flags) == 0
        assert ind.units_evaluated == 0
