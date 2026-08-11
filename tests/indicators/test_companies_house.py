"""Tests for UK ingest party cross-reference and Companies House indicators."""

from __future__ import annotations

import json
from datetime import datetime

from django.test import TestCase

from uncorrupt.connectors.base import RawArtifact
from uncorrupt.indicators.catalog.i006_incorporation_proximity import IncorporationProximity
from uncorrupt.indicators.catalog.i007_value_vs_company_size import ValueVsCompanySize
from uncorrupt.indicators.catalog.i008_dormancy_delinquency import DormancyDelinquency
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.register.loader import load_locale
from uncorrupt.staging.companies_house import ingest_ch_bulk_csv, resolve_suppliers
from uncorrupt.staging.ingest import ingest_artifacts
from uncorrupt.staging.models import Award, Company, SupplierResolution, Tender


def _make_uk_release(
    ocid: str = "ocds-1",
    supplier_name: str = "Test Supplier Ltd",
    supplier_id: str = "1",
    party_scheme: str = "GB-COH",
    party_id: str = "12345678",
    award_value_gbp: float = 50000,
    award_date: str = "2024-06-15T00:00:00",
) -> bytes:
    """Build a minimal UK OCDS release with supplier identifier in parties[]."""
    return json.dumps(
        {
            "ocid": ocid,
            "parties": [
                {
                    "id": "buyer-1",
                    "name": "Test Buyer",
                    "roles": ["buyer"],
                    "identifier": {"scheme": "GB-GOR", "id": "GOR-1"},
                },
                {
                    "id": supplier_id,
                    "name": supplier_name,
                    "roles": ["supplier"],
                    "identifier": {"scheme": party_scheme, "id": party_id},
                },
            ],
            "tender": {
                "id": f"tender-{ocid}",
                "title": "Test Tender",
                "status": "complete",
                "procurementMethod": "open",
                "value": {"amount": 100000, "currency": "GBP"},
            },
            "awards": [
                {
                    "id": f"award-{ocid}",
                    "suppliers": [{"id": supplier_id, "name": supplier_name}],
                    "value": {"amount": award_value_gbp, "currency": "GBP"},
                    "date": award_date,
                },
            ],
        }
    )


class UKIngestPartyCrossRefTest(TestCase):
    """Verify the parties[] cross-reference fix for UK supplier identifiers."""

    def test_uk_award_gets_gb_coh_from_parties(self):
        """A UK award with GB-COH in parties[] must populate supplier_id_scheme/id."""
        payload = _make_uk_release(
            party_scheme="GB-COH",
            party_id="01234567",
        )
        artifact = RawArtifact(
            source_url="https://example.com",
            media_type="application/json",
            payload=payload.encode(),
        )
        ingest_artifacts("uk_contracts_finder", [artifact])

        award = Award.objects.get(source_id="uk_contracts_finder", award_id="award-ocds-1")
        assert award.supplier_id_scheme == "GB-COH"
        assert award.supplier_id == "01234567"

    def test_uk_award_with_no_identifier_stays_null(self):
        """A supplier party with no identifier must leave supplier_id null."""
        payload = _make_uk_release(
            party_scheme="",
            party_id="",
        )
        artifact = RawArtifact(
            source_url="https://example.com",
            media_type="application/json",
            payload=payload.encode(),
        )
        ingest_artifacts("uk_contracts_finder", [artifact])

        award = Award.objects.get(source_id="uk_contracts_finder", award_id="award-ocds-1")
        assert award.supplier_id is None or award.supplier_id == ""

    def test_uk_award_falls_back_to_name_match(self):
        """If supplier id doesn't match a party, fall back to name."""
        payload = json.dumps(
            {
                "ocid": "ocds-name-fallback",
                "parties": [
                    {
                        "id": "party-x",
                        "name": "Named Supplier Ltd",
                        "roles": ["supplier"],
                        "identifier": {"scheme": "GB-COH", "id": "99999999"},
                    },
                ],
                "tender": {
                    "id": "tender-name-fallback",
                    "title": "Test",
                    "value": {"amount": 10000, "currency": "GBP"},
                },
                "awards": [
                    {
                        "id": "award-name-fallback",
                        "suppliers": [{"id": "WRONG-ID", "name": "Named Supplier Ltd"}],
                        "value": {"amount": 5000, "currency": "GBP"},
                        "date": "2024-01-01T00:00:00",
                    },
                ],
            }
        )
        artifact = RawArtifact(
            source_url="https://example.com",
            media_type="application/json",
            payload=payload.encode(),
        )
        ingest_artifacts("uk_contracts_finder", [artifact])

        award = Award.objects.get(source_id="uk_contracts_finder", award_id="award-name-fallback")
        assert award.supplier_id_scheme == "GB-COH"
        assert award.supplier_id == "99999999"


def _setup_company(
    company_number: str = "12345678",
    company_name: str = "Test Supplier Ltd",
    company_status: str = "Active",
    incorporation_date=None,
    accounts_category: str = "FULL",
    accounts_last_made_up_date=None,
) -> Company:
    """Create a Company in the DB for testing."""
    from datetime import date

    return Company.objects.create(
        company_number=company_number,
        company_name=company_name,
        company_status=company_status,
        incorporation_date=incorporation_date or date(2020, 1, 1),
        accounts_category=accounts_category,
        accounts_last_made_up_date=accounts_last_made_up_date or date(2024, 1, 1),
        normalised_name=company_name.upper().strip(),
    )


def _setup_award_with_resolution(
    supplier_name: str = "Test Supplier Ltd",
    company_number: str = "12345678",
    match_method: str = "identifier",
    match_confidence: float = 1.0,
    award_value_gbp: float = 50000,
    award_date: str = "2024-06-15T00:00:00",
) -> tuple[Award, SupplierResolution]:
    """Create an Award + SupplierResolution for indicator testing."""

    tender = Tender.objects.create(
        source_id="uk_contracts_finder",
        tender_id="test-tender-1",
        source_url="https://example.com",
    )
    award = Award.objects.create(
        source_id="uk_contracts_finder",
        tender_id="test-tender-1",
        award_id="test-award-1",
        tender_ref=tender,
        supplier_name=supplier_name,
        supplier_id_scheme="GB-COH" if match_method == "identifier" else None,
        supplier_id=company_number if match_method == "identifier" else None,
        currency="GBP",
        value_amount_cents=int(award_value_gbp * 100),
        status="active",
        award_date=_parse_dt(award_date),
        raw_json={},
    )
    company = Company.objects.get(company_number=company_number)
    res = SupplierResolution.objects.create(
        source_id="uk_contracts_finder",
        supplier_name=supplier_name,
        supplier_id_scheme="GB-COH" if match_method == "identifier" else None,
        supplier_id=company_number if match_method == "identifier" else None,
        company=company,
        company_number=company_number,
        match_confidence=match_confidence,
        match_method=match_method,
    )
    return award, res


def _parse_dt(s: str):
    from datetime import UTC

    return datetime.fromisoformat(s).replace(tzinfo=UTC)


def _make_ctx():
    return EvaluationContext(locale=load_locale("gb"), source_id="uk_contracts_finder")


class I006IncorporationProximityTest(TestCase):
    """i006: supplier incorporated < N days before award."""

    def test_incorporated_30_days_before_flags(self):
        """Incorporated 30 days before award MUST flag."""
        from datetime import date

        _setup_company(
            company_number="12345678",
            incorporation_date=date(2024, 5, 16),  # 30 days before award
        )
        _setup_award_with_resolution(
            company_number="12345678",
            award_date="2024-06-15T00:00:00",
        )

        ind = IncorporationProximity()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 1
        assert "30 days" in flags[0].explanation
        assert ind.units_evaluated > 0

    def test_incorporated_5_years_before_does_not_flag(self):
        """Incorporated 5 years before award MUST NOT flag."""
        from datetime import date

        _setup_company(
            company_number="12345678",
            incorporation_date=date(2019, 6, 15),  # 5 years before award
        )
        _setup_award_with_resolution(
            company_number="12345678",
            award_date="2024-06-15T00:00:00",
        )

        ind = IncorporationProximity()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 0

    def test_no_resolution_does_not_flag(self):
        """An award with no resolution must not flag."""
        _setup_company(company_number="12345678")
        # Create an award with a supplier name that has NO resolution
        tender = Tender.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-no-res",
            source_url="https://example.com",
        )
        Award.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-no-res",
            award_id="test-award-no-res",
            tender_ref=tender,
            supplier_name="Unresolved Supplier",
            currency="GBP",
            value_amount_cents=5000000,
            status="active",
            award_date=_parse_dt("2024-06-15T00:00:00"),
            raw_json={},
        )

        ind = IncorporationProximity()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 0


class I007ValueVsCompanySizeTest(TestCase):
    """i007: award value large relative to company accounts category."""

    def test_dormant_company_large_award_flags(self):
        """A dormant company with a very large award MUST flag."""
        _setup_company(
            company_number="12345678",
            accounts_category="DORMANT",
        )
        _setup_award_with_resolution(
            company_number="12345678",
            award_value_gbp=500000,  # £500k — above any dormant threshold
        )

        ind = ValueVsCompanySize()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 1
        assert "dormant" in flags[0].explanation.lower()

    def test_large_company_same_award_does_not_flag(self):
        """A large company (FULL accounts) with the same award MUST NOT flag."""
        _setup_company(
            company_number="12345678",
            accounts_category="FULL",
        )
        _setup_award_with_resolution(
            company_number="12345678",
            award_value_gbp=500000,
        )

        ind = ValueVsCompanySize()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 0

    def test_micro_entity_small_award_does_not_flag(self):
        """A micro-entity with a small award (below threshold) MUST NOT flag."""
        _setup_company(
            company_number="12345678",
            accounts_category="MICRO-ENTITY",
        )
        _setup_award_with_resolution(
            company_number="12345678",
            award_value_gbp=10000,  # £10k — below £50k threshold
        )

        ind = ValueVsCompanySize()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 0


class I008DormancyDelinquencyTest(TestCase):
    """i008: dormant accounts or overdue/stale filings."""

    def test_overdue_filings_flag(self):
        """Overdue filings (accounts > 18 months old) MUST flag."""
        from datetime import date

        _setup_company(
            company_number="12345678",
            accounts_category="FULL",
            accounts_last_made_up_date=date(2022, 1, 1),  # ~30 months before award
        )
        _setup_award_with_resolution(
            company_number="12345678",
            award_date="2024-06-15T00:00:00",
        )

        ind = DormancyDelinquency()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 1
        assert "stale" in flags[0].explanation.lower()

    def test_current_filings_do_not_flag(self):
        """Current filings MUST NOT flag."""
        from datetime import date

        _setup_company(
            company_number="12345678",
            accounts_category="FULL",
            accounts_last_made_up_date=date(2024, 3, 1),  # 3 months before award
        )
        _setup_award_with_resolution(
            company_number="12345678",
            award_date="2024-06-15T00:00:00",
        )

        ind = DormancyDelinquency()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 0

    def test_dormant_accounts_flag(self):
        """Dormant accounts MUST flag regardless of filing date."""
        from datetime import date

        _setup_company(
            company_number="12345678",
            accounts_category="DORMANT",
            accounts_last_made_up_date=date(2024, 3, 1),  # recent
        )
        _setup_award_with_resolution(
            company_number="12345678",
            award_date="2024-06-15T00:00:00",
        )

        ind = DormancyDelinquency()
        flags = list(ind.evaluate(_make_ctx()))
        assert len(flags) == 1
        assert "dormant" in flags[0].explanation.lower()


class SupplierResolutionTest(TestCase):
    """Resolution: identifier and name matching with uniqueness guard."""

    def test_identifier_match_confidence_1(self):
        """Identifier match → confidence 1.0."""
        _setup_company(company_number="12345678", company_name="Test Supplier Ltd")
        # Create an award with a GB-COH supplier_id
        tender = Tender.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-res",
            source_url="https://example.com",
        )
        Award.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-res",
            award_id="test-award-res",
            tender_ref=tender,
            supplier_name="Test Supplier Ltd",
            supplier_id_scheme="GB-COH",
            supplier_id="12345678",
            currency="GBP",
            value_amount_cents=5000000,
            status="active",
            award_date=_parse_dt("2024-06-15T00:00:00"),
            raw_json={},
        )

        result = resolve_suppliers("uk_contracts_finder")
        assert result["tier1_identifier"] == 1

        res = SupplierResolution.objects.get(supplier_name="Test Supplier Ltd")
        assert res.match_confidence == 1.0
        assert res.match_method == "identifier"

    def test_ambiguous_name_low_confidence_no_match(self):
        """A deliberately ambiguous name → low confidence and NOT silently treated as a match."""
        # Create two companies with the same name
        _setup_company(company_number="11111111", company_name="Ambiguous Ltd")
        _setup_company(company_number="22222222", company_name="Ambiguous Ltd")

        tender = Tender.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-amb",
            source_url="https://example.com",
        )
        Award.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-amb",
            award_id="test-award-amb",
            tender_ref=tender,
            supplier_name="Ambiguous Ltd",
            currency="GBP",
            value_amount_cents=5000000,
            status="active",
            award_date=_parse_dt("2024-06-15T00:00:00"),
            raw_json={},
        )

        resolve_suppliers("uk_contracts_finder")
        res = SupplierResolution.objects.get(supplier_name="Ambiguous Ltd")
        assert res.match_confidence == 0.0
        assert res.match_method is None
        assert "Ambiguous" in (res.normalisation_note or "")

    def test_exact_name_unique_match(self):
        """Exact name match when exactly one company has that name → confidence 0.9."""
        _setup_company(company_number="12345678", company_name="Unique Supplier Ltd")

        tender = Tender.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-uniq",
            source_url="https://example.com",
        )
        Award.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-uniq",
            award_id="test-award-uniq",
            tender_ref=tender,
            supplier_name="Unique Supplier Ltd",
            currency="GBP",
            value_amount_cents=5000000,
            status="active",
            award_date=_parse_dt("2024-06-15T00:00:00"),
            raw_json={},
        )

        result = resolve_suppliers("uk_contracts_finder")
        assert result["tier2_exact_name"] == 1

        res = SupplierResolution.objects.get(supplier_name="Unique Supplier Ltd")
        assert res.match_confidence == 0.9
        assert res.match_method == "exact_name"

    def test_exact_name_sole_active_match(self):
        """Multiple companies share a name but only one is Active → match the active one."""
        _setup_company(
            company_number="11111111",
            company_name="Shared Name Ltd",
            company_status="Active",
        )
        _setup_company(
            company_number="22222222",
            company_name="Shared Name Ltd",
            company_status="Dissolved",
        )

        tender = Tender.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-sole-active",
            source_url="https://example.com",
        )
        Award.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-sole-active",
            award_id="test-award-sole-active",
            tender_ref=tender,
            supplier_name="Shared Name Ltd",
            currency="GBP",
            value_amount_cents=5000000,
            status="active",
            award_date=_parse_dt("2024-06-15T00:00:00"),
            raw_json={},
        )

        result = resolve_suppliers("uk_contracts_finder")
        assert result["tier2_exact_name"] == 1

        res = SupplierResolution.objects.get(supplier_name="Shared Name Ltd")
        assert res.match_confidence == 0.9
        assert res.match_method == "exact_name"
        assert res.company_number == "11111111"
        assert "sole active" in (res.normalisation_note or "").lower()

    def test_exact_name_two_active_no_match(self):
        """Multiple active companies share a name → uniqueness guard, no match."""
        _setup_company(
            company_number="11111111",
            company_name="Dup Active Ltd",
            company_status="Active",
        )
        _setup_company(
            company_number="22222222",
            company_name="Dup Active Ltd",
            company_status="Active",
        )

        tender = Tender.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-two-active",
            source_url="https://example.com",
        )
        Award.objects.create(
            source_id="uk_contracts_finder",
            tender_id="test-tender-two-active",
            award_id="test-award-two-active",
            tender_ref=tender,
            supplier_name="Dup Active Ltd",
            currency="GBP",
            value_amount_cents=5000000,
            status="active",
            award_date=_parse_dt("2024-06-15T00:00:00"),
            raw_json={},
        )

        result = resolve_suppliers("uk_contracts_finder")
        assert result["tier2_exact_name"] == 0
        assert result["unmatched"] >= 1

        res = SupplierResolution.objects.get(supplier_name="Dup Active Ltd")
        assert res.match_confidence == 0.0
        assert res.match_method is None


class Phase4KnownCaseCoverageTest(TestCase):
    """Phase 4 — indicator coverage test against known UK PPE cases.

    These are adjudicated High Court cases, all public record. The contracts
    were 2020 emergency PCR reg 32(2)(c) direct awards, so i001/i005 firing
    on them proves nothing (direct award was near-universal). Only the
    company-join indicators count.

    Coverage table (expected):
    | case          | expected indicator | flagged? |
    | PPE Medpro    | i006               | yes      |
    | PestFix       | i007               | yes      |
    | Ayanda        | NONE               | no       | (documented gap)

    IMPORTANT: the frozen snapshot is 2026 data — these 2020 contracts are
    NOT in it. These tests use synthetic fixtures built from the known public
    facts (incorporation date, award date, value, accounts category).
    """

    def test_ppe_medpro_caught_by_i006(self):
        """PPE Medpro: incorporated 2020-05-12, won GBP 81m on 2020-06-12 (31 days).

        MUST be caught by i006 (incorporation proximity).
        """
        from datetime import date

        _setup_company(
            company_number="12345678",
            company_name="PPE Medpro Ltd",
            incorporation_date=date(2020, 5, 12),
            accounts_category="SMALL",
        )
        _setup_award_with_resolution(
            supplier_name="PPE Medpro Ltd",
            company_number="12345678",
            award_value_gbp=81_000_000,
            award_date="2020-06-12T00:00:00",
        )

        i006 = IncorporationProximity()
        flags = list(i006.evaluate(_make_ctx()))
        assert len(flags) == 1
        assert "31 days" in flags[0].explanation

    def test_pestfix_caught_by_i007(self):
        """PestFix: small pest-control firm won ~GBP 340m.

        Older company, so i006 does NOT catch it.
        MUST be caught by i007 (value vs company size).
        """
        from datetime import date

        _setup_company(
            company_number="12345678",
            company_name="PestFix Systems Ltd",
            incorporation_date=date(2007, 1, 1),
            accounts_category="SMALL",
        )
        _setup_award_with_resolution(
            supplier_name="PestFix Systems Ltd",
            company_number="12345678",
            award_value_gbp=340_000_000,
            award_date="2020-04-01T00:00:00",
        )

        # i006 must NOT catch it (company is 13 years old)
        i006 = IncorporationProximity()
        i006_flags = list(i006.evaluate(_make_ctx()))
        assert len(i006_flags) == 0

        # i007 MUST catch it (small company, huge award)
        i007 = ValueVsCompanySize()
        i007_flags = list(i007.evaluate(_make_ctx()))
        assert len(i007_flags) == 1
        assert "small" in i007_flags[0].explanation.lower()

    def test_ayanda_caught_by_neither(self):
        """Ayanda Capital (~GBP 252m): caught by neither i006 nor i007.

        Documented gap — not papered over.
        """
        from datetime import date

        _setup_company(
            company_number="12345678",
            company_name="Ayanda Capital Ltd",
            incorporation_date=date(2015, 1, 1),  # 5 years old — not new
            accounts_category="FULL",  # not dormant/micro/small
        )
        _setup_award_with_resolution(
            supplier_name="Ayanda Capital Ltd",
            company_number="12345678",
            award_value_gbp=252_000_000,
            award_date="2020-04-01T00:00:00",
        )

        i006 = IncorporationProximity()
        i006_flags = list(i006.evaluate(_make_ctx()))
        assert len(i006_flags) == 0

        i007 = ValueVsCompanySize()
        i007_flags = list(i007.evaluate(_make_ctx()))
        assert len(i007_flags) == 0

        # i008 also should not catch it (FULL accounts, current filings)
        i008 = DormancyDelinquency()
        i008_flags = list(i008.evaluate(_make_ctx()))
        assert len(i008_flags) == 0


class ChBulkCsvColumnMapTest(TestCase):
    """Regression: ingest_ch_bulk_csv must populate incorporation_date from the real
    Companies House bulk header 'IncorporationDate' (which normalises to
    'incorporationdate' — no 'company' prefix), not just the speculative
    'companyincorporationdate' key the live header never matches. Before the fix,
    5.7M companies ingested with incorporation_date=NULL and i006 returned 0 flags."""

    def test_incorporation_date_populated_from_real_ch_header(self):
        import tempfile
        from datetime import date

        csv_text = (
            "CompanyName, CompanyNumber,CompanyStatus,IncorporationDate,"
            "Accounts.AccountCategory,Accounts.LastMadeUpDate,SICCode.SicText_1\n"
            "SHELL LTD,12345678,Active,2024-05-01,MICRO_ENTITY,2025-05-01,70229\n"
            "OLD LTD,87654321,Active,1999-01-01,FULL,2024-12-31,99999\n"
        )
        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False) as f:
            f.write(csv_text)
            path = f.name
        n = ingest_ch_bulk_csv(path, snapshot_date=date(2026, 7, 1))
        assert n == 2
        shell = Company.objects.get(company_number="12345678")
        old = Company.objects.get(company_number="87654321")
        assert shell.incorporation_date == date(2024, 5, 1), (
            f"incorporation_date not parsed from 'IncorporationDate' header: "
            f"{shell.incorporation_date!r}"
        )
        assert old.incorporation_date == date(1999, 1, 1)
        assert shell.accounts_category == "MICRO_ENTITY"
