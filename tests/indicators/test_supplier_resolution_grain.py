"""Regression tests for the SupplierResolution grain defect (D1).

`SupplierResolution` is unique on (source_id, supplier_name), but the write
path `resolve_suppliers()` (src/uncorrupt/staging/companies_house.py) writes
one row per distinct (supplier_name, supplier_id_scheme, supplier_id) triple,
keyed only on (source_id, supplier_name) in `update_or_create`. When one
supplier_name carries TWO different GB-COH supplier_ids, the second write
overwrites the first — only one company_number survives for that name.

The read path — every indicator (i006/i007/i008/i009) — then looks up the
resolution by `award.supplier_name` alone, never consulting
`award.supplier_id`. So an award that declares its own GB-COH id can be
scored against the WRONG company.

This test performs a real ingest-fixture -> resolve_suppliers() ->
IncorporationProximity().evaluate() round trip to catch that
misattribution end-to-end, plus a fixed-denominator guard that would have
caught the PR #29 denominator regression.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from uncorrupt.indicators.catalog.i006_incorporation_proximity import IncorporationProximity
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.register.models import LocaleProfile
from uncorrupt.staging.companies_house import resolve_suppliers
from uncorrupt.staging.models import Award, Company, Tender

SOURCE = "uk_contracts_finder"


def _make_ctx() -> EvaluationContext:
    locale = LocaleProfile(code="gb", procedure_metadata={})
    return EvaluationContext(locale=locale, source_id=SOURCE)


def _setup_company(
    company_number: str,
    company_name: str,
    incorporation_date: date,
    status: str = "Active",
):
    return Company.objects.create(
        company_number=company_number,
        company_name=company_name,
        company_status=status,
        incorporation_date=incorporation_date,
        accounts_category="FULL",
        normalised_name=company_name.upper().strip(),
    )


def _setup_tender(tender_id: str, buyer: str = "Test Council"):
    return Tender.objects.create(
        source_id=SOURCE,
        tender_id=tender_id,
        ocid=f"ocds-test-{tender_id}",
        title="Test procurement",
        status="active",
        procurement_method="open",
        currency="GBP",
        buyer_name=buyer,
        buyer_country="GB",
        raw_json={},
    )


def _setup_award(
    tender,
    award_id: str,
    supplier_name: str,
    supplier_id: str,
    value_cents: int | None,
    award_date_str: str,
):
    return Award.objects.create(
        source_id=SOURCE,
        tender_id=tender.tender_id,
        tender_ref=tender,
        award_id=award_id,
        supplier_name=supplier_name,
        supplier_id_scheme="GB-COH",
        supplier_id=supplier_id,
        currency="GBP",
        value_amount_cents=value_cents,
        status="active",
        award_date=f"{award_date_str}T00:00:00Z",
        raw_json={},
    )


class TestSupplierResolutionGrain:
    """D1: SupplierResolution grain must be (source_id, supplier_id), not
    (source_id, supplier_name) — a name-keyed resolution misattributes
    awards to the wrong company when identifiers collide either direction."""

    @pytest.mark.django_db
    def test_award_with_recent_incorporation_is_flagged_by_its_own_id(self):
        """GIVEN two companies sharing one supplier_name — one incorporated
        10 days before its award (inside the 90-day window), the other
        incorporated ~15 years before its award (outside the window) — and
        awards that each carry their OWN distinct GB-COH supplier_id
        WHEN resolve_suppliers() then IncorporationProximity().evaluate() run
        THEN exactly one flag is emitted, for the award whose OWN company is
        newly incorporated (A1), not the collapsed/other award."""
        award_date = date(2024, 6, 15)
        recent_incorp = award_date - timedelta(days=10)
        old_incorp = award_date - timedelta(days=15 * 365)

        _setup_company("00000001", "SHARED NAME LTD", recent_incorp)
        _setup_company("00000002", "SHARED NAME LTD", old_incorp)

        t1 = _setup_tender("T1")
        t2 = _setup_tender("T2")
        _setup_award(t1, "A1", "SHARED NAME LTD", "00000001", 5000000, "2024-06-15")
        _setup_award(t2, "A2", "SHARED NAME LTD", "00000002", 5000000, "2024-06-15")

        resolve_suppliers(SOURCE)
        ind = IncorporationProximity()
        flags = list(ind.evaluate(_make_ctx()))

        assert len(flags) == 1, (
            f"expected 1 flag, got {len(flags)}: {[f.subject_ref for f in flags]}"
        )
        assert flags[0].subject_ref == "T1:A1"

    @pytest.mark.django_db
    def test_award_with_old_incorporation_not_among_flags(self):
        """GIVEN the same two-company/two-award fixture as above WHEN
        resolve_suppliers() then IncorporationProximity().evaluate() run
        THEN award A2 (company incorporated ~15 years before its award) is
        NOT among the emitted flags' subject_refs."""
        award_date = date(2024, 6, 15)
        recent_incorp = award_date - timedelta(days=10)
        old_incorp = award_date - timedelta(days=15 * 365)

        _setup_company("00000001", "SHARED NAME LTD", recent_incorp)
        _setup_company("00000002", "SHARED NAME LTD", old_incorp)

        t1 = _setup_tender("T1")
        t2 = _setup_tender("T2")
        _setup_award(t1, "A1", "SHARED NAME LTD", "00000001", 5000000, "2024-06-15")
        _setup_award(t2, "A2", "SHARED NAME LTD", "00000002", 5000000, "2024-06-15")

        resolve_suppliers(SOURCE)
        ind = IncorporationProximity()
        flags = list(ind.evaluate(_make_ctx()))

        subject_refs = [f.subject_ref for f in flags]
        assert "T2:A2" not in subject_refs

    @pytest.mark.django_db
    def test_one_supplier_id_two_names_both_resolve_and_neither_flags(self):
        """GIVEN one supplier_id ("00000003") declared under two different
        supplier_names ("GAMMA LTD" and "GAMMA LIMITED") on two awards, and
        the company incorporated long ago (outside the proximity window)
        WHEN resolve_suppliers() then IncorporationProximity().evaluate() run
        THEN both awards are resolvable and counted in units_evaluated, and
        neither produces a flag."""
        award_date = date(2024, 6, 15)
        old_incorp = award_date - timedelta(days=20 * 365)
        _setup_company("00000003", "GAMMA LTD", old_incorp)

        t3 = _setup_tender("T3")
        t4 = _setup_tender("T4")
        _setup_award(t3, "A3", "GAMMA LTD", "00000003", 3000000, "2024-06-15")
        _setup_award(t4, "A4", "GAMMA LIMITED", "00000003", 3000000, "2024-06-15")

        resolve_suppliers(SOURCE)
        ind = IncorporationProximity()
        flags = list(ind.evaluate(_make_ctx()))

        assert ind.units_evaluated == 2
        assert len(flags) == 0

    @pytest.mark.django_db
    def test_denominator_includes_zero_value_resolvable_awards(self):
        """GIVEN 5 active awards with resolvable suppliers — two with
        value_amount_cents=0 and three with normal positive values, none
        within the incorporation-proximity window WHEN
        IncorporationProximity().evaluate() is fully consumed THEN
        units_evaluated equals exactly 5 — zero-value awards are counted in
        the denominator, never silently excluded.

        Note: `Award.value_amount_cents` is `BigIntegerField(default=0)`
        with no `null=True` on the model (src/uncorrupt/staging/models.py),
        so a NULL value is not representable at the DB layer and is not
        exercised here; zero is the falsy value that could plausibly be
        mishandled by a denominator that does `if award.value_amount_cents:`.
        """
        award_date = date(2024, 6, 15)
        old_incorp = award_date - timedelta(days=10 * 365)
        _setup_company("00000010", "DELTA LTD", old_incorp)

        fixtures = [
            ("T10", "A10", 0),
            ("T11", "A11", 0),
            ("T12", "A12", 1000000),
            ("T13", "A13", 2000000),
            ("T14", "A14", 3000000),
        ]
        for tid, aid, value in fixtures:
            t = _setup_tender(tid)
            _setup_award(t, aid, "DELTA LTD", "00000010", value, "2024-06-15")

        resolve_suppliers(SOURCE)
        ind = IncorporationProximity()
        list(ind.evaluate(_make_ctx()))

        assert ind.units_evaluated == 5
