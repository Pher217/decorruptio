"""Tests for D2: Award.value_kind semantics.

A shared OCDS framework ceiling is aggregate money declared across N suppliers,
not each supplier's own money. Ingest marks it `value_kind="shared_ceiling"`
while preserving the declared amount; i007 (the value-sensitive indicator)
must abstain on such rows rather than flag or drop them from the denominator.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime

from django.test import TestCase

from uncorrupt.connectors.base import RawArtifact
from uncorrupt.indicators.catalog.i007_value_vs_company_size import ValueVsCompanySize
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.register.loader import load_locale
from uncorrupt.staging.ingest import ingest_artifacts
from uncorrupt.staging.models import Award, Company, SupplierResolution, Tender


def _make_uk_release(
    ocid: str,
    suppliers: list[dict],
    award_value_gbp: float = 1_000_000_000,
    award_date: str = "2024-06-15T00:00:00",
) -> bytes:
    """Build a minimal UK OCDS release with an award listing `suppliers`."""
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
                *[
                    {
                        "id": s["id"],
                        "name": s["name"],
                        "roles": ["supplier"],
                        "identifier": {"scheme": "GB-COH", "id": s["company_number"]},
                    }
                    for s in suppliers
                ],
            ],
            "tender": {
                "id": f"tender-{ocid}",
                "title": "Test Tender",
                "status": "complete",
                "procurementMethod": "open",
                "value": {"amount": award_value_gbp, "currency": "GBP"},
            },
            "awards": [
                {
                    "id": f"award-{ocid}",
                    "status": "active",
                    "suppliers": [{"id": s["id"], "name": s["name"]} for s in suppliers],
                    "value": {"amount": award_value_gbp, "currency": "GBP"},
                    "date": award_date,
                },
            ],
        }
    ).encode()


def _ingest(payload: bytes) -> None:
    artifact = RawArtifact(
        source_url="https://example.com",
        media_type="application/json",
        payload=payload,
    )
    ingest_artifacts("uk_contracts_finder", [artifact])


class AwardValueKindIngestTest(TestCase):
    """Ingest marks value_kind by supplier count, preserving the declared amount."""

    def test_multi_supplier_award_marks_all_rows_shared_ceiling_with_full_value(self):
        """GIVEN an OCDS award with 3 suppliers sharing a GBP 1,000,000,000 ceiling
        WHEN the release is ingested
        THEN all 3 Award rows are value_kind="shared_ceiling" and each still
        carries the full declared value_amount_cents."""
        suppliers = [
            {"id": f"sup-{i}", "name": f"Supplier {i} Ltd", "company_number": f"{10000000 + i}"}
            for i in range(3)
        ]
        _ingest(_make_uk_release("ocds-multi", suppliers, award_value_gbp=1_000_000_000))

        awards = list(
            Award.objects.filter(
                source_id="uk_contracts_finder", tender_id="tender-ocds-multi"
            ).order_by("award_id")
        )
        assert len(awards) == 3
        for award in awards:
            assert award.value_kind == "shared_ceiling"
            assert award.value_amount_cents == 100_000_000_000

    def test_single_supplier_award_marks_per_supplier(self):
        """GIVEN an OCDS award with exactly 1 supplier
        WHEN the release is ingested
        THEN the Award row is value_kind="per_supplier"."""
        suppliers = [{"id": "sup-solo", "name": "Solo Supplier Ltd", "company_number": "20000000"}]
        _ingest(_make_uk_release("ocds-solo", suppliers, award_value_gbp=500_000))

        award = Award.objects.get(source_id="uk_contracts_finder", tender_id="tender-ocds-solo")
        assert award.value_kind == "per_supplier"


def _setup_company(company_number: str, accounts_category: str = "MICRO-ENTITY") -> Company:
    return Company.objects.create(
        company_number=company_number,
        company_name=f"Company {company_number}",
        company_status="Active",
        incorporation_date=date(2020, 1, 1),
        accounts_category=accounts_category,
        accounts_last_made_up_date=date(2024, 1, 1),
        normalised_name=f"COMPANY {company_number}",
    )


def _setup_award(
    tender_id: str,
    award_id: str,
    supplier_name: str,
    company_number: str,
    value_cents: int,
    value_kind: str,
) -> Award:
    tender = Tender.objects.create(
        source_id="uk_contracts_finder",
        tender_id=tender_id,
        source_url="https://example.com",
    )
    award = Award.objects.create(
        source_id="uk_contracts_finder",
        tender_id=tender_id,
        award_id=award_id,
        tender_ref=tender,
        supplier_name=supplier_name,
        supplier_id_scheme="GB-COH",
        supplier_id=company_number,
        currency="GBP",
        value_amount_cents=value_cents,
        value_kind=value_kind,
        status="active",
        award_date=datetime(2024, 6, 15, tzinfo=UTC),
        raw_json={},
    )
    company = Company.objects.get(company_number=company_number)
    SupplierResolution.objects.create(
        source_id="uk_contracts_finder",
        supplier_name=supplier_name,
        supplier_id_scheme="GB-COH",
        supplier_id=company_number,
        company=company,
        company_number=company_number,
        match_confidence=1.0,
        match_method="identifier",
    )
    return award


def _make_ctx() -> EvaluationContext:
    return EvaluationContext(locale=load_locale("gb"), source_id="uk_contracts_finder")


class I007ShareCeilingAbstentionTest(TestCase):
    """i007 must abstain (not flag) on shared_ceiling awards, without moving the denominator."""

    def test_shared_ceiling_award_that_would_otherwise_flag_emits_no_flag(self):
        """GIVEN a micro-entity company with an award of GBP 1,000,000,000 marked
        value_kind="shared_ceiling" (which would flag if it were per_supplier)
        WHEN i007 evaluates
        THEN no Flag is emitted for that award."""
        _setup_company("30000000", accounts_category="MICRO-ENTITY")
        _setup_award(
            tender_id="tender-shared",
            award_id="award-shared",
            supplier_name="Shared Supplier Ltd",
            company_number="30000000",
            value_cents=100_000_000_000_00,
            value_kind="shared_ceiling",
        )

        ind = ValueVsCompanySize()
        flags = list(ind.evaluate(_make_ctx()))

        assert flags == []

    def test_same_award_as_per_supplier_does_flag(self):
        """GIVEN the identical micro-entity + GBP 1,000,000,000 award, but
        value_kind="per_supplier"
        WHEN i007 evaluates
        THEN it DOES flag — proving the abstention above is what suppressed it,
        not the threshold or company setup."""
        _setup_company("30000001", accounts_category="MICRO-ENTITY")
        _setup_award(
            tender_id="tender-per-supplier",
            award_id="award-per-supplier",
            supplier_name="Per Supplier Ltd",
            company_number="30000001",
            value_cents=100_000_000_000_00,
            value_kind="per_supplier",
        )

        ind = ValueVsCompanySize()
        flags = list(ind.evaluate(_make_ctx()))

        assert len(flags) == 1

    def test_units_evaluated_unchanged_by_abstention(self):
        """GIVEN one shared_ceiling award and one ordinary per_supplier award,
        both resolvable
        WHEN i007 evaluates
        THEN units_evaluated counts BOTH — the abstained award stays in the
        denominator."""
        _setup_company("30000002", accounts_category="MICRO-ENTITY")
        _setup_award(
            tender_id="tender-denom-1",
            award_id="award-denom-1",
            supplier_name="Denom Supplier A Ltd",
            company_number="30000002",
            value_cents=100_000_000_000_00,
            value_kind="shared_ceiling",
        )
        _setup_company("30000003", accounts_category="MICRO-ENTITY")
        _setup_award(
            tender_id="tender-denom-2",
            award_id="award-denom-2",
            supplier_name="Denom Supplier B Ltd",
            company_number="30000003",
            value_cents=10_000_00,
            value_kind="per_supplier",
        )

        ind = ValueVsCompanySize()
        list(ind.evaluate(_make_ctx()))

        assert ind.units_evaluated == 2

    def test_units_unscoreable_counts_exactly_the_shared_ceiling_awards(self):
        """GIVEN two shared_ceiling awards and one per_supplier award, all resolvable
        WHEN i007 evaluates
        THEN units_unscoreable equals exactly 2."""
        _setup_company("30000004", accounts_category="MICRO-ENTITY")
        _setup_award(
            tender_id="tender-unscoreable-1",
            award_id="award-unscoreable-1",
            supplier_name="Unscoreable Supplier A Ltd",
            company_number="30000004",
            value_cents=100_000_000_000_00,
            value_kind="shared_ceiling",
        )
        _setup_company("30000005", accounts_category="MICRO-ENTITY")
        _setup_award(
            tender_id="tender-unscoreable-2",
            award_id="award-unscoreable-2",
            supplier_name="Unscoreable Supplier B Ltd",
            company_number="30000005",
            value_cents=100_000_000_000_00,
            value_kind="shared_ceiling",
        )
        _setup_company("30000006", accounts_category="MICRO-ENTITY")
        _setup_award(
            tender_id="tender-unscoreable-3",
            award_id="award-unscoreable-3",
            supplier_name="Unscoreable Supplier C Ltd",
            company_number="30000006",
            value_cents=10_000_00,
            value_kind="per_supplier",
        )

        ind = ValueVsCompanySize()
        list(ind.evaluate(_make_ctx()))

        assert ind.units_unscoreable == 2
