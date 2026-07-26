"""Tests for the UK Parliament register of interests ingest (Phase 1.3).

Verifies the core invariants:
- Interest registrationDate maps to Edge.valid_from
- A category-specific EndDate maps to Edge.valid_to
- Ambiguous counterparty name (2+ companies) never guesses — no edge
- A company-number counterparty gives match_confidence=1.0
- A value band (no exact figure) never populates amount_cents
"""

import json
from pathlib import Path

import httpx
import pytest

from uncorrupt.graph.models import Attestation, Entity
from uncorrupt.graph.parliament_interests import (
    fetch_parliament_interests,
    ingest_parliament_interests_json,
    list_registers,
)
from uncorrupt.staging.models import Company

MEMBER = {
    "id": 4504,
    "nameDisplayAs": "Wes Streeting",
    "nameListAs": "Streeting, Wes",
    "house": "Commons",
    "memberFrom": "Ilford North",
    "party": "Labour",
}


def _interest(
    interest_id: int,
    category_name: str,
    fields: list[dict],
    registration_date: str = "2026-07-13",
    member: dict | None = MEMBER,
    child_interests: list[dict] | None = None,
) -> dict:
    return {
        "id": interest_id,
        "summary": f"interest {interest_id}",
        "parentInterestId": None,
        "registrationDate": registration_date,
        "publishedDate": registration_date,
        "updatedDates": [],
        "category": {"id": 1, "number": "1", "name": category_name, "type": "Commons"},
        "member": member,
        "fields": fields,
        "childInterests": child_interests,
    }


def _field(name: str, value, field_type: str = "String", currency: str | None = None) -> dict:
    type_info = {"currencyCode": currency} if currency else None
    return {
        "name": name,
        "description": name,
        "type": field_type,
        "typeInfo": type_info,
        "value": value,
    }


def _write_json(tmp_path: Path, items: list[dict]) -> Path:
    json_path = tmp_path / "parliament_interests.json"
    json_path.write_text(json.dumps(items))
    return json_path


@pytest.mark.django_db
class TestParliamentInterestsIngest:
    def test_registration_date_maps_to_valid_from(self, tmp_path):
        """A donation interest's registrationDate becomes Edge.valid_from."""
        Company.objects.create(
            company_number="12410514",
            company_name="PPE Medpro Ltd",
            normalised_name="PPE MEDPRO LTD",
        )
        items = [
            _interest(
                16217,
                "Donations and other support (including loans) for activities as an MP",
                [
                    _field("DonorCompanyName", "PPE Medpro Ltd"),
                    _field("DonorCompanyIdentifier", "12410514"),
                    _field("DonorStatus", "Company"),
                    _field("Value", "10000.00", "Decimal", "GBP"),
                ],
                registration_date="2026-07-13",
            )
        ]
        json_path = _write_json(tmp_path, items)

        ingest_parliament_interests_json(json_path)

        edge = Attestation.objects.get(source_reference="16217").edge
        assert edge.valid_from.isoformat() == "2026-07-13"

    def test_end_date_maps_to_valid_to(self, tmp_path):
        """A category-specific EndDate field becomes Edge.valid_to."""
        Company.objects.create(
            company_number="00000010", company_name="Employer Ltd", normalised_name="EMPLOYER LTD"
        )
        items = [
            _interest(
                16306,
                "Employment and earnings",
                [
                    _field("PayerName", "Employer Ltd"),
                    _field("PayerIsPrivateIndividual", False, "Boolean"),
                    _field("EndDate", "2026-08-19", "DateOnly"),
                    _field("Value", "4556.33", "Decimal", "GBP"),
                ],
                registration_date="2026-07-13",
            )
        ]
        json_path = _write_json(tmp_path, items)

        ingest_parliament_interests_json(json_path)

        edge = Attestation.objects.get(source_reference="16306").edge
        assert edge.valid_to.isoformat() == "2026-08-19"

    def test_ambiguous_counterparty_name_creates_no_edge(self, tmp_path):
        """Two companies sharing a normalised name must never be guessed (uniqueness guard)."""
        Company.objects.create(
            company_number="00000001", company_name="Example Ltd", normalised_name="EXAMPLE LTD"
        )
        Company.objects.create(
            company_number="00000002", company_name="Example Ltd", normalised_name="EXAMPLE LTD"
        )
        items = [
            _interest(
                16400,
                "Donations and other support (including loans) for activities as an MP",
                [
                    _field("DonorCompanyName", "Example Ltd"),
                    _field("DonorStatus", "Company"),
                    _field("Value", "500.00", "Decimal", "GBP"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 0
        assert summary["unmatched_counterparty"] == 1
        assert Attestation.objects.filter(source_reference="16400").count() == 0

    def test_company_number_counterparty_gives_full_confidence(self, tmp_path):
        """A donor row with a company registration number joins with zero name matching."""
        Company.objects.create(
            company_number="12410514",
            company_name="PPE Medpro Ltd",
            normalised_name="PPE MEDPRO LTD",
        )
        items = [
            _interest(
                16217,
                "Donations and other support (including loans) for activities as an MP",
                [
                    _field("DonorCompanyName", "PPE Medpro Ltd"),
                    _field("DonorCompanyIdentifier", "12410514"),
                    _field("DonorStatus", "Company"),
                    _field("Value", "10000.00", "Decimal", "GBP"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 1
        attestation = Attestation.objects.get(source_reference="16217")
        edge = attestation.edge
        assert attestation.match_confidence == 1.0
        assert attestation.match_method == "identifier"
        assert edge.amount_cents == 1000000
        assert edge.currency == "GBP"

    def test_value_band_does_not_populate_amount_cents(self, tmp_path):
        """A shareholding value band never gets converted into an invented amount_cents figure."""
        items = [
            _interest(
                16500,
                "Shareholdings",
                [
                    _field(
                        "ShareholdingThreshold",
                        "(ii) Other shareholdings, valued at more than £70,000",
                    ),
                    _field("OrganisationName", "Lockhouse Systems Limited"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 1
        edge = Attestation.objects.get(source_reference="16500").edge
        assert edge.amount_cents is None
        assert (
            edge.properties["value_band"] == "(ii) Other shareholdings, valued at more than £70,000"
        )

    def test_individual_donor_creates_no_entity_or_edge(self, tmp_path):
        """Individual donors are out of scope (ADR-004 D1) — no Entity/Edge created."""
        items = [
            _interest(
                16218,
                "Donations and other support (including loans) for activities as an MP",
                [
                    _field("DonorName", "Anthony P Clarke"),
                    _field("DonorStatus", "Individual"),
                    _field("Value", "50000.00", "Decimal", "GBP"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["skipped_private_individual"] == 1
        assert Attestation.objects.filter(source_reference="16218").count() == 0
        assert not Entity.objects.filter(name="Anthony P Clarke").exists()

    def test_family_member_category_creates_no_entity_or_edge(self, tmp_path):
        """Family members' interests are excluded entirely — not the member's own public role."""
        items = [
            _interest(
                16600,
                "Family members employed",
                [
                    _field("PersonName", "Dr Maria Psatha"),
                    _field("JobTitle", "Office Manager"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["skipped_family"] == 1
        assert Attestation.objects.filter(source_reference="16600").count() == 0
        assert not Entity.objects.filter(name="Dr Maria Psatha").exists()

    def test_child_interest_inherits_parent_payer_name(self, tmp_path):
        """A payment child interest resolves its counterparty from the parent's PayerName."""
        Company.objects.create(
            company_number="00000020", company_name="Law Firm LLP", normalised_name="LAW FIRM LLP"
        )
        child = _interest(
            16312,
            "Employment and earnings - Ongoing paid employment",
            [_field("Value", "4556.33", "Decimal", "GBP")],
            registration_date="2026-07-13",
        )
        parent = _interest(
            16306,
            "Employment and earnings",
            [
                _field("PayerName", "Law Firm LLP"),
                _field("PayerIsPrivateIndividual", False, "Boolean"),
            ],
            child_interests=[child],
        )
        json_path = _write_json(tmp_path, [parent])

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 1
        edge = Attestation.objects.get(source_reference="16312").edge
        assert edge.target_entity.name == "Law Firm LLP"
        attestation = edge.attestations.get()
        assert attestation.match_confidence == 0.9
        assert attestation.match_method == "exact_name"

    def test_named_non_company_counterparty_resolves_at_reduced_confidence(self, tmp_path):
        """A named payer with no Companies House match still records at reduced confidence."""
        items = [
            _interest(
                16700,
                "Employment and earnings",
                [
                    _field("PayerName", "IPSA"),
                    _field("PayerIsPrivateIndividual", False, "Boolean"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 1
        edge = Attestation.objects.get(source_reference="16700").edge
        attestation = edge.attestations.get()
        assert attestation.match_confidence == 0.5
        assert attestation.match_method == "name_only"
        assert edge.target_entity.entity_type == "regulated_entity"

    def test_member_entity_created_with_registry_id(self, tmp_path):
        """The member becomes a person Entity resolved by Parliament member ID, not name."""
        Company.objects.create(
            company_number="12410514",
            company_name="PPE Medpro Ltd",
            normalised_name="PPE MEDPRO LTD",
        )
        items = [
            _interest(
                16217,
                "Donations and other support (including loans) for activities as an MP",
                [
                    _field("DonorCompanyName", "PPE Medpro Ltd"),
                    _field("DonorCompanyIdentifier", "12410514"),
                    _field("DonorStatus", "Company"),
                    _field("Value", "10000.00", "Decimal", "GBP"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        ingest_parliament_interests_json(json_path)

        member_entity = Entity.objects.get(
            registry_scheme="UK-PARLIAMENT-MEMBER", registry_id="4504"
        )
        assert member_entity.entity_type == "person"
        assert member_entity.name == "Wes Streeting"
        assert member_entity.role_description == "MP for Ilford North"

    def test_unresolved_same_named_counterparties_stay_distinct_across_interests(self, tmp_path):
        """Two different interests naming an unresolvable 'Acme Ltd' must never merge.

        Merging would attach one MP's declared interest to an entity shared
        with an unrelated interest that happens to name the same string —
        duplication is the correct outcome when identity can't be proven.
        """
        items = [
            _interest(
                17001,
                "Employment and earnings",
                [
                    _field("PayerName", "Acme Ltd"),
                    _field("PayerIsPrivateIndividual", False, "Boolean"),
                ],
            ),
            _interest(
                17002,
                "Employment and earnings",
                [
                    _field("PayerName", "Acme Ltd"),
                    _field("PayerIsPrivateIndividual", False, "Boolean"),
                ],
            ),
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 2
        edge_1 = Attestation.objects.get(source_reference="17001").edge
        edge_2 = Attestation.objects.get(source_reference="17002").edge
        assert edge_1.target_entity.pk != edge_2.target_entity.pk
        assert edge_1.target_entity.registry_scheme == "UK-PARLIAMENT-UNRESOLVED"

    def test_unresolvable_company_number_is_retained_in_properties(self, tmp_path):
        """A supplied-but-unresolvable company number is kept, not discarded."""
        items = [
            _interest(
                17010,
                "Donations and other support (including loans) for activities as an MP",
                [
                    _field("DonorCompanyName", "Untracked Donor Ltd"),
                    _field("DonorCompanyIdentifier", "99999999"),
                    _field("DonorStatus", "Company"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 1
        edge = Attestation.objects.get(source_reference="17010").edge
        assert edge.properties["declared_company_number"] == "99999999"
        assert edge.target_entity.registry_scheme == "UK-PARLIAMENT-UNRESOLVED"

    def test_inverted_interval_end_before_start_is_not_stored(self, tmp_path):
        """An EndDate before registrationDate is bad data, not a valid claim."""
        Company.objects.create(
            company_number="00000011",
            company_name="Employer Two Ltd",
            normalised_name="EMPLOYER TWO LTD",
        )
        items = [
            _interest(
                17020,
                "Employment and earnings",
                [
                    _field("PayerName", "Employer Two Ltd"),
                    _field("PayerIsPrivateIndividual", False, "Boolean"),
                    _field("EndDate", "2026-06-19", "DateOnly"),
                ],
                registration_date="2026-07-13",
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["inverted_interval"] == 1
        edge = Attestation.objects.get(source_reference="17020").edge
        assert edge.valid_to is None
        assert edge.properties["end_date_before_registration_date"] == "2026-06-19"

    def test_unknown_donor_status_is_skipped_not_treated_as_organisation(self, tmp_path):
        """A DonorName with no positive organisation classification is skipped (fail-closed)."""
        items = [
            _interest(
                17030,
                "Donations and other support (including loans) for activities as an MP",
                [
                    _field("DonorName", "Ambiguous Donor"),
                    _field("DonorStatus", "Other"),
                    _field("Value", "1000.00", "Decimal", "GBP"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["skipped_unclassified_counterparty"] == 1
        assert summary["matched"] == 0
        assert Attestation.objects.filter(source_reference="17030").count() == 0
        assert not Entity.objects.filter(name="Ambiguous Donor").exists()

    def test_payer_with_no_private_individual_classification_is_skipped(self, tmp_path):
        """A PayerName with PayerIsPrivateIndividual missing must not default to organisation."""
        items = [
            _interest(
                17031,
                "Employment and earnings",
                [
                    _field("PayerName", "Unclassified Payer"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["skipped_unclassified_counterparty"] == 1
        assert not Entity.objects.filter(name="Unclassified Payer").exists()

    def test_value_band_parses_structured_min_bound(self, tmp_path):
        """A 'more than £X' band parses into value_band_min_cents without inventing a max."""
        items = [
            _interest(
                17040,
                "Shareholdings",
                [
                    _field(
                        "ShareholdingThreshold",
                        "(ii) Other shareholdings, valued at more than £70,000",
                    ),
                    _field("OrganisationName", "Band Systems Limited"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        ingest_parliament_interests_json(json_path)

        edge = Attestation.objects.get(source_reference="17040").edge
        assert edge.amount_cents is None
        assert edge.properties["value_band_min_cents"] == 7000000
        assert "value_band_max_cents" not in edge.properties

    def test_value_band_with_no_monetary_figure_parses_no_bounds(self, tmp_path):
        """A percentage-based band never invents monetary bounds."""
        items = [
            _interest(
                17041,
                "Shareholdings",
                [
                    _field(
                        "ShareholdingThreshold",
                        "(i) Shareholdings: over 15% of issued share capital",
                    ),
                    _field("OrganisationName", "Percent Holdings Ltd"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        ingest_parliament_interests_json(json_path)

        edge = Attestation.objects.get(source_reference="17041").edge
        assert "value_band_min_cents" not in edge.properties
        assert "value_band_max_cents" not in edge.properties
        assert (
            edge.properties["value_band"] == "(i) Shareholdings: over 15% of issued share capital"
        )


class TestParliamentInterestsFetch:
    def test_fetch_uses_valid_sort_order(self, tmp_path, monkeypatch):
        """The fetch must never send the invalid PublishingDateAscending sort value."""
        captured_urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_urls.append(str(request.url))
            return httpx.Response(200, json={"items": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))

        fetch_parliament_interests(tmp_path, client=client)

        assert captured_urls
        for url in captured_urls:
            assert "SortOrder=PublishingDateAscending" not in url
        assert "SortOrder=PublishingDateDescending" in captured_urls[0]

    def test_fetch_passes_register_id_when_supplied(self, tmp_path, monkeypatch):
        """A supplied register_id is forwarded to the API as RegisterId."""
        captured_urls = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured_urls.append(str(request.url))
            return httpx.Response(200, json={"items": []})

        client = httpx.Client(transport=httpx.MockTransport(handler))

        fetch_parliament_interests(tmp_path, register_id=804, client=client)

        assert "RegisterId=804" in captured_urls[0]

    def test_list_registers_paginates_and_parses_items(self, tmp_path):
        """list_registers enumerates published register documents via the Registers endpoint."""

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "totalResults": 2,
                    "items": [
                        {"id": 804, "publishedDate": "2026-07-13", "type": "Commons"},
                        {"id": 803, "publishedDate": "2026-06-29", "type": "Commons"},
                    ],
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))

        registers = list_registers(client=client)

        assert len(registers) == 2
        assert registers[0].register_id == 804
        assert registers[0].published_date == "2026-07-13"
