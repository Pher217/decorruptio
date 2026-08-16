"""Tests for `AwardResolution` and the rewritten `resolve_suppliers()` (ADR-012 D1).

`SupplierResolution` is unique on (source_id, supplier_name): when two awards
declare different GB-COH supplier_ids under one shared supplier_name, the
second write silently overwrote the first. `AwardResolution` is one row per
Award instead, so each award's own identifier drives its own resolution.

See also `tests/indicators/test_supplier_resolution_grain.py` for the
end-to-end (ingest -> resolve -> indicator) regression this fix targets.
"""

from __future__ import annotations

from datetime import date

import pytest

from uncorrupt.indicators.catalog._shared import confidence_note
from uncorrupt.indicators.catalog.i006_incorporation_proximity import IncorporationProximity
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.register.models import LocaleProfile
from uncorrupt.staging.companies_house import resolve_suppliers
from uncorrupt.staging.models import Award, AwardResolution, Company, SupplierResolution, Tender

SOURCE = "uk_contracts_finder"


def _make_ctx() -> EvaluationContext:
    locale = LocaleProfile(code="gb", procedure_metadata={})
    return EvaluationContext(locale=locale, source_id=SOURCE)


def _make_company(
    company_number: str,
    company_name: str,
    status: str = "Active",
    incorporation_date: date | None = None,
) -> Company:
    return Company.objects.create(
        company_number=company_number,
        company_name=company_name,
        company_status=status,
        incorporation_date=incorporation_date or date(2020, 1, 1),
        accounts_category="FULL",
        normalised_name=company_name.upper().strip(),
    )


def _make_tender(tender_id: str) -> Tender:
    return Tender.objects.create(
        source_id=SOURCE,
        tender_id=tender_id,
        ocid=f"ocds-{tender_id}",
        title="Test procurement",
        status="active",
        procurement_method="open",
        currency="GBP",
        buyer_name="Test Council",
        buyer_country="GB",
        raw_json={},
    )


def _make_award(
    tender: Tender,
    award_id: str,
    supplier_name: str | None,
    supplier_id: str | None = None,
    supplier_id_scheme: str | None = None,
    value_cents: int = 5_000_000,
    award_date_str: str = "2024-06-15",
) -> Award:
    return Award.objects.create(
        source_id=SOURCE,
        tender_id=tender.tender_id,
        tender_ref=tender,
        award_id=award_id,
        supplier_name=supplier_name,
        supplier_id_scheme=supplier_id_scheme,
        supplier_id=supplier_id,
        currency="GBP",
        value_amount_cents=value_cents,
        status="active",
        award_date=f"{award_date_str}T00:00:00Z",
        raw_json={},
    )


@pytest.mark.django_db
class TestAwardOwnIdentifierWins:
    def test_award_resolves_from_its_own_id_despite_shared_name(self):
        """GIVEN two companies sharing one supplier_name, each with an award
        carrying its OWN distinct GB-COH supplier_id WHEN resolve_suppliers()
        runs THEN each award's AwardResolution.company_number matches its OWN
        award's id -- neither inherits the other's resolution via the shared
        name (the D1 grain fix)."""
        _make_company("00000001", "SHARED NAME LTD")
        _make_company("00000002", "SHARED NAME LTD")
        t1 = _make_tender("T1")
        t2 = _make_tender("T2")
        a1 = _make_award(t1, "A1", "SHARED NAME LTD", "00000001", "GB-COH")
        a2 = _make_award(t2, "A2", "SHARED NAME LTD", "00000002", "GB-COH")

        resolve_suppliers(SOURCE)

        res1 = AwardResolution.objects.get(award=a1)
        res2 = AwardResolution.objects.get(award=a2)
        assert res1.company_number == "00000001"
        assert res2.company_number == "00000002"


@pytest.mark.django_db
class TestUnmatchedIdentifierStaysInDenominator:
    def test_unresolved_gb_coh_id_still_gets_non_null_company_number(self):
        """GIVEN an award whose GB-COH id has no matching row in the CH bulk
        snapshot WHEN resolve_suppliers() runs THEN it still gets an
        AwardResolution with a non-null company_number at confidence 0.0 --
        it stays in the denominator, exactly as SupplierResolution did
        before this fix."""
        tender = _make_tender("T1")
        award = _make_award(tender, "A1", "Ghost Supplier Ltd", "09999999", "GB-COH")

        resolve_suppliers(SOURCE)

        res = AwardResolution.objects.get(award=award)
        assert res.company_number == "09999999"
        assert res.company is None
        assert res.match_confidence == 0.0
        assert res.match_method is None


@pytest.mark.django_db
class TestNameTierResolution:
    def test_id_less_award_resolves_via_name_tier_at_point_nine(self):
        """GIVEN an award with no GB-COH id but a supplier_name that
        uniquely matches one Company WHEN resolve_suppliers() runs THEN its
        AwardResolution is resolved via the name tier at confidence 0.9."""
        _make_company("00000003", "GAMMA LTD")
        tender = _make_tender("T1")
        award = _make_award(tender, "A1", "GAMMA LTD")

        resolve_suppliers(SOURCE)

        res = AwardResolution.objects.get(award=award)
        assert res.company_number == "00000003"
        assert res.match_confidence == 0.9
        assert res.match_method == "exact_name"

    def test_ambiguous_name_among_dissolved_companies_is_not_evaluable(self):
        """GIVEN an id-less award whose supplier_name matches TWO companies,
        neither of them active, WHEN resolve_suppliers() runs THEN its
        AwardResolution has company_number=None -- excluded from the
        denominator by the uniqueness guard."""
        _make_company("00000004", "AMBIGUOUS LTD", status="Dissolved")
        _make_company("00000005", "AMBIGUOUS LTD", status="Dissolved")
        tender = _make_tender("T1")
        award = _make_award(tender, "A1", "AMBIGUOUS LTD")

        resolve_suppliers(SOURCE)

        res = AwardResolution.objects.get(award=award)
        assert res.company_number is None
        assert res.match_method is None


@pytest.mark.django_db
class TestIdentifierResolutionsNotInSupplierResolution:
    def test_supplier_resolution_holds_only_name_tier_rows(self):
        """GIVEN a fixture mixing one GB-COH-identified award and one
        id-less, name-resolved award WHEN resolve_suppliers() runs THEN
        SupplierResolution contains only the name-tier row -- identifier
        resolutions are written exclusively to AwardResolution."""
        _make_company("00000006", "ID SUPPLIER LTD")
        _make_company("00000007", "NAME SUPPLIER LTD")
        t1 = _make_tender("T1")
        t2 = _make_tender("T2")
        _make_award(t1, "A1", "ID SUPPLIER LTD", "00000006", "GB-COH")
        _make_award(t2, "A2", "NAME SUPPLIER LTD")

        resolve_suppliers(SOURCE)

        assert AwardResolution.objects.count() == 2
        supplier_names = set(SupplierResolution.objects.values_list("supplier_name", flat=True))
        assert supplier_names == {"NAME SUPPLIER LTD"}


@pytest.mark.django_db
class TestResolveSuppliersIdempotent:
    def test_running_twice_produces_no_duplicate_rows(self):
        """GIVEN a fixture of identifier and name-tier awards WHEN
        resolve_suppliers() runs twice in a row THEN AwardResolution and
        SupplierResolution each still hold exactly one row per award/name,
        with identical field values -- no duplicates, no drift."""
        _make_company("00000008", "ID SUPPLIER LTD")
        _make_company("00000009", "NAME SUPPLIER LTD")
        t1 = _make_tender("T1")
        t2 = _make_tender("T2")
        _make_award(t1, "A1", "ID SUPPLIER LTD", "00000008", "GB-COH")
        _make_award(t2, "A2", "NAME SUPPLIER LTD")

        resolve_suppliers(SOURCE)
        first_award_resolutions = list(
            AwardResolution.objects.order_by("id").values(
                "award_id", "company_number", "match_confidence", "match_method"
            )
        )
        first_supplier_resolutions = list(
            SupplierResolution.objects.order_by("id").values(
                "supplier_name", "company_number", "match_confidence", "match_method"
            )
        )

        resolve_suppliers(SOURCE)

        assert AwardResolution.objects.count() == 2
        assert SupplierResolution.objects.count() == 1
        second_award_resolutions = list(
            AwardResolution.objects.order_by("id").values(
                "award_id", "company_number", "match_confidence", "match_method"
            )
        )
        second_supplier_resolutions = list(
            SupplierResolution.objects.order_by("id").values(
                "supplier_name", "company_number", "match_confidence", "match_method"
            )
        )
        assert first_award_resolutions == second_award_resolutions
        assert first_supplier_resolutions == second_supplier_resolutions


@pytest.mark.django_db
class TestNoAwardResolutionRowsRaises:
    def test_indicator_raises_when_awards_exist_but_no_resolutions_do(self):
        """GIVEN a source with an Award but ZERO AwardResolution rows (e.g.
        after a truncate, before resolve_suppliers() has run) WHEN an
        indicator evaluates THEN it raises RuntimeError naming the source,
        instead of silently reporting units_evaluated=0."""
        tender = _make_tender("T1")
        _make_award(tender, "A1", "Some Supplier Ltd")

        ind = IncorporationProximity()
        with pytest.raises(RuntimeError, match=SOURCE):
            list(ind.evaluate(_make_ctx()))


class TestConfidenceNote:
    def test_identifier_match_prints_a_caveat(self):
        """GIVEN an identifier match (confidence 1.0, method 'identifier')
        WHEN confidence_note() formats it THEN it prints the method and
        confidence -- it no longer returns an empty string for identifier
        matches, since an identifier match can still be wrong if the
        award's own GB-COH id was misresolved upstream."""
        note = confidence_note(1.0, "identifier")

        assert note != ""
        assert "identifier" in note
        assert "1.0" in note


@pytest.mark.django_db
class TestNamelessAwardExcludedFromIndicator:
    def test_nameless_gb_coh_id_gets_resolution_but_is_not_evaluated(self):
        """GIVEN an active award with supplier_name=None and a valid GB-COH
        supplier_id WHEN resolve_suppliers() runs THEN it still gets an
        AwardResolution row with a non-null company_number, but i006 does
        NOT count it in units_evaluated."""
        _make_company("00000010", "HIDDEN LTD")
        tender = _make_tender("T1")
        award = _make_award(tender, "A1", None, "00000010", "GB-COH")

        resolve_suppliers(SOURCE)

        res = AwardResolution.objects.get(award=award)
        assert res.company_number == "00000010"

        ind = IncorporationProximity()
        list(ind.evaluate(_make_ctx()))
        assert ind.units_evaluated == 0


@pytest.mark.django_db
class TestWhitespaceGbCohIdStaysInDenominator:
    def test_whitespace_only_gb_coh_id_is_evaluable(self):
        """GIVEN an active award with supplier_name='Whitespace Ltd' and a
        whitespace-only GB-COH supplier_id ' ' WHEN resolve_suppliers()
        runs THEN its AwardResolution.company_number is not None and i006
        counts it in units_evaluated, preserving the legacy behaviour."""
        tender = _make_tender("T1")
        award = _make_award(tender, "A1", "Whitespace Ltd", " ", "GB-COH")

        resolve_suppliers(SOURCE)

        res = AwardResolution.objects.get(award=award)
        assert res.company_number is not None

        ind = IncorporationProximity()
        list(ind.evaluate(_make_ctx()))
        assert ind.units_evaluated == 1
