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

import uncorrupt.graph.parliament_interests as parliament_interests_module
from uncorrupt.core.errors import RegisterError
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


def _donor_group(
    name: str, is_private_individual: bool | None, value: str | None = None
) -> list[dict]:
    """One donor entry inside a "Visits outside the UK" `Donors` field.

    Mirrors the live API shape verified 2026-08-04: a donor group is a
    field-list (`Name`, `IsPrivateIndividual`, `Value`, ...) in the same
    shape as a top-level `fields` list, nested under the `Donors` field's
    own `values` key rather than its `value` key.
    """
    fields = [
        _field("Name", name),
        _field("IsPrivateIndividual", is_private_individual, "Boolean"),
    ]
    if value is not None:
        fields.append(_field("Value", value, "Decimal", "GBP"))
    return fields


def _donors_field(donor_groups: list[list[dict]]) -> dict:
    return {
        "name": "Donors",
        "description": "Donors and value of visits",
        "type": "Donor[]",
        "typeInfo": None,
        "value": None,
        "values": donor_groups,
    }


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

    def test_company_number_with_gleif_and_coh_entities_resolves_to_coh(self, tmp_path):
        """A company_number shared by GB-COH and GLEIF-LEI Entities resolves to GB-COH.

        Regression test: Entity.objects.get_or_create(entity_type="company",
        company_number=...) without registry_scheme raised
        MultipleObjectsReturned whenever GLEIF held a separate Entity for
        the same company (ADR-006: duplicate over merge, never collapsed).
        """
        Company.objects.create(
            company_number="12410514",
            company_name="PPE Medpro Ltd",
            normalised_name="PPE MEDPRO LTD",
        )
        Entity.objects.create(
            entity_type="company",
            registry_scheme="GB-COH",
            registry_id="12410514",
            name="PPE Medpro Ltd",
            company_number="12410514",
        )
        Entity.objects.create(
            entity_type="company",
            registry_scheme="GLEIF-LEI",
            registry_id="529900XYZ1234567890A",
            name="PPE Medpro Ltd",
            company_number="12410514",
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

        assert summary["ambiguous_company_number"] == 0
        assert summary["matched"] == 1
        coh_entity = Entity.objects.get(registry_scheme="GB-COH", registry_id="12410514")
        edge = Attestation.objects.get(source_reference="16217").edge
        assert edge.target_entity_id == coh_entity.id
        # The GLEIF entity must still exist untouched — never merged
        assert Entity.objects.filter(
            registry_scheme="GLEIF-LEI", registry_id="529900XYZ1234567890A"
        ).exists()

    def test_ambiguous_company_number_counter_increments_run_continues(self, tmp_path, monkeypatch):
        """A MultipleObjectsReturned during resolution is counted, not fatal to the run.

        Simulates the defensive catch around per-interest resolution: even
        if resolution raises Entity.MultipleObjectsReturned, the ingest
        counts it and keeps processing rather than losing the whole run to
        one bad row.
        """
        import uncorrupt.graph.parliament_interests as parliament_interests_module

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

        def _raise_ambiguous(*args, **kwargs):
            raise Entity.MultipleObjectsReturned("simulated ambiguity")

        monkeypatch.setattr(
            parliament_interests_module, "_resolve_counterparty_entity", _raise_ambiguous
        )

        summary = ingest_parliament_interests_json(json_path)

        assert summary["ambiguous_company_number"] == 1
        assert summary["matched"] == 0
        assert summary["total"] == 1

    def test_visit_donor_nested_in_donor_array_is_extracted(self, tmp_path):
        """A "Visits outside the UK" interest's sponsor lives in a nested
        `Donor[]` field, not a flat `DonorName`/`PayerName`/`OrganisationName`
        field. Regression test: before `_counterparty_groups` was added, the
        flat `_fields_by_name`/`_raw_field` helpers only ever read a field's
        own `value` (null here) and never its nested `values`, so this
        interest fell through to `skipped_no_counterparty` with zero edges
        created — reproduced live 2026-08-04 (410/410 Visits interests)."""
        items = [
            _interest(
                20001,
                "Visits outside the UK",
                [
                    _field("Purpose", "Fact finding visit"),
                    _field("StartDate", "2025-10-06", "DateOnly"),
                    _field("EndDate", "2025-10-10", "DateOnly"),
                    _donors_field(
                        [
                            _donor_group(
                                "Caabu (Council for Arab-British Understanding)", False, "1260.00"
                            )
                        ]
                    ),
                ],
                registration_date="2025-10-20",
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 1
        assert summary["skipped_no_counterparty"] == 0
        edge = Attestation.objects.get(source_reference="20001").edge
        assert edge.target_entity.name == "Caabu (Council for Arab-British Understanding)"
        assert edge.amount_cents == 126000
        assert edge.valid_from.isoformat() == "2025-10-20"

    def test_visit_with_multiple_donors_creates_one_edge_per_donor(self, tmp_path):
        """A visit jointly funded by two organisations creates two distinct
        edges, not one edge with the second donor silently dropped."""
        items = [
            _interest(
                20002,
                "Visits outside the UK",
                [
                    _field("Purpose", "Conference"),
                    _field("StartDate", "2026-06-02", "DateOnly"),
                    _field("EndDate", "2026-06-07", "DateOnly"),
                    _donors_field(
                        [
                            _donor_group("First Sponsor Org", False, "500.00"),
                            _donor_group("Second Sponsor Org", False, "300.00"),
                        ]
                    ),
                ],
                registration_date="2026-06-20",
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 2
        attestations = Attestation.objects.filter(source_reference="20002")
        assert attestations.count() == 2
        target_names = {a.edge.target_entity.name for a in attestations}
        assert target_names == {"First Sponsor Org", "Second Sponsor Org"}
        edge_ids = {a.edge_id for a in attestations}
        assert len(edge_ids) == 2

    def test_visit_with_private_individual_donor_creates_no_person_entity(self, tmp_path):
        """A visit donor positively flagged as a private individual must never
        become a person Entity or edge (ADR-004 D1) — even through the new
        nested `Donor[]` code path, not just the flat-field path already
        covered by `test_individual_donor_creates_no_entity_or_edge`."""
        items = [
            _interest(
                20003,
                "Visits outside the UK",
                [
                    _field("Purpose", "Private visit"),
                    _field("StartDate", "2026-03-01", "DateOnly"),
                    _field("EndDate", "2026-03-05", "DateOnly"),
                    _donors_field([_donor_group("Jane Private Citizen", True, "2000.00")]),
                ],
                registration_date="2026-03-10",
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["skipped_private_individual"] == 1
        assert summary["matched"] == 0
        assert Attestation.objects.filter(source_reference="20003").count() == 0
        assert not Entity.objects.filter(name="Jane Private Citizen").exists()

    def test_visit_donor_with_missing_private_individual_flag_is_skipped(self, tmp_path):
        """A donor group with no `IsPrivateIndividual` flag must never default
        to organisation (fail-closed), mirroring
        `test_payer_with_no_private_individual_classification_is_skipped`."""
        items = [
            _interest(
                20004,
                "Visits outside the UK",
                [
                    _field("Purpose", "Unclassified visit"),
                    _field("StartDate", "2026-03-01", "DateOnly"),
                    _field("EndDate", "2026-03-05", "DateOnly"),
                    _donors_field([_donor_group("Ambiguous Sponsor", None, "1000.00")]),
                ],
                registration_date="2026-03-10",
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["skipped_unclassified_counterparty"] == 1
        assert summary["matched"] == 0
        assert not Entity.objects.filter(name="Ambiguous Sponsor").exists()

    def test_limited_liability_partnership_donor_status_is_organisation(self, tmp_path):
        """DonorStatus "Limited Liability Partnership" is a real, live-verified
        value (e.g. "The Ivors Academy") this allowlist was previously
        missing — an LLP is never a private individual. Mirrors
        `ec_donations.ORGANISATION_DONOR_STATUSES`, which already allows it."""
        items = [
            _interest(
                20005,
                "Gifts, benefits and hospitality from UK sources",
                [
                    _field("DonorName", "The Ivors Academy"),
                    _field("DonorStatus", "Limited Liability Partnership"),
                    _field("Value", "1314.00", "Decimal", "GBP"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 1
        assert summary["skipped_unclassified_counterparty"] == 0
        edge = Attestation.objects.get(source_reference="20005").edge
        assert edge.target_entity.name == "The Ivors Academy"

    def test_registered_party_donor_status_is_organisation(self, tmp_path):
        """DonorStatus "Registered Party" (a registered political party) is a
        real, live-verified value this allowlist was previously missing — a
        registered party is never a private individual."""
        items = [
            _interest(
                20006,
                "Donations and other support (including loans) for activities as an MP",
                [
                    _field("DonorName", "Example Registered Party"),
                    _field("DonorStatus", "Registered Party"),
                    _field("Value", "5000.00", "Decimal", "GBP"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 1
        assert summary["skipped_unclassified_counterparty"] == 0
        edge = Attestation.objects.get(source_reference="20006").edge
        assert edge.target_entity.name == "Example Registered Party"

    def test_trust_donor_status_remains_excluded(self, tmp_path):
        """DonorStatus "Trust" is deliberately NOT added to the allowlist: a
        trust can be a private family trust rather than an institutional
        one, and `ec_donations.ORGANISATION_DONOR_STATUSES` excludes it too
        — fail closed, never guess an ambiguous status into an organisation."""
        items = [
            _interest(
                20007,
                "Gifts, benefits and hospitality from UK sources",
                [
                    _field("DonorName", "Example Family Trust"),
                    _field("DonorStatus", "Trust"),
                    _field("Value", "800.00", "Decimal", "GBP"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["skipped_unclassified_counterparty"] == 1
        assert summary["matched"] == 0
        assert not Entity.objects.filter(name="Example Family Trust").exists()

    def test_name_only_counterparty_resolves_despite_legal_suffix_mismatch(self, tmp_path):
        """A declared organisation name differing from the Companies House legal
        name only by legal-form suffix ("Ltd" vs "Limited") must still resolve
        to the real Company, not fall back to a UK-PARLIAMENT-UNRESOLVED
        placeholder.

        Regression test: `_resolve_counterparty_entity`'s name-only path tried
        only `_normalise_name` (case/whitespace only, no suffix/punctuation
        stripping) as its sole match attempt. Verified live 2026-08-04 against
        the real graph: 22 of the 25 ever-ingested Commons declared_interest
        edges resolved to a UK-PARLIAMENT-UNRESOLVED placeholder instead of an
        already-known real Company for exactly this reason -- e.g. the
        declared "DODS GROUP LTD" never matched Companies House's real "DODS
        GROUP LIMITED" (company number 01262354).
        """
        Company.objects.create(
            company_number="01262354",
            company_name="Dods Group Limited",
            normalised_name="DODS GROUP LIMITED",
        )
        items = [
            _interest(
                17100,
                "Employment and earnings",
                [
                    _field("PayerName", "Dods Group Ltd"),
                    _field("PayerIsPrivateIndividual", False, "Boolean"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 1
        edge = Attestation.objects.get(source_reference="17100").edge
        assert edge.target_entity.registry_scheme == "GB-COH"
        assert edge.target_entity.company_number == "01262354"
        attestation = edge.attestations.get()
        assert attestation.match_method == "normalised_name"
        assert attestation.match_confidence == 0.8

    def test_name_only_counterparty_ambiguous_after_suffix_stripping_is_not_guessed(self, tmp_path):
        """Two companies that only collide once legal-form suffixes are
        stripped must never be guessed through — same "duplication over
        merging" discipline as the exact-match ambiguity guard, applied to
        the suffix-tolerant fallback too."""
        Company.objects.create(
            company_number="00000001",
            company_name="Example Ltd",
            normalised_name="EXAMPLE LTD",
        )
        Company.objects.create(
            company_number="00000002",
            company_name="Example Limited",
            normalised_name="EXAMPLE LIMITED",
        )
        items = [
            _interest(
                17101,
                "Employment and earnings",
                [
                    _field("PayerName", "Example PLC"),
                    _field("PayerIsPrivateIndividual", False, "Boolean"),
                ],
            )
        ]
        json_path = _write_json(tmp_path, items)

        summary = ingest_parliament_interests_json(json_path)

        assert summary["matched"] == 0
        assert summary["unmatched_counterparty"] == 1
        assert Attestation.objects.filter(source_reference="17101").count() == 0


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

    def test_fetch_paginates_across_multiple_full_pages_without_premature_stop(self, tmp_path):
        """Regression guard for the pagination-defect class already seen twice
        in this codebase (EC's `start` parameter silently ignored, returning
        byte-identical pages forever; a page returning fewer items than
        requested ending the loop before the true end of the corpus). A
        real 3-page fetch (two full pages of `page_size`, one partial) must
        make exactly 3 requests with strictly increasing, non-repeating
        `Skip` values, and must return the full 45 items across all pages —
        never stopping after the first full page nor looping forever on a
        server that ignores `Skip`.
        """
        page_size = 20
        pages = {
            0: [{"id": i} for i in range(20)],
            20: [{"id": i} for i in range(20, 40)],
            40: [{"id": i} for i in range(40, 45)],
        }
        requested_skips: list[int] = []

        def handler(request: httpx.Request) -> httpx.Response:
            skip = int(request.url.params["Skip"])
            requested_skips.append(skip)
            return httpx.Response(200, json={"items": pages.get(skip, [])})

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = fetch_parliament_interests(
            tmp_path, page_size=page_size, polite_delay_seconds=0, client=client
        )

        assert requested_skips == [0, 20, 40]
        assert result.item_count == 45


class TestParliamentInterestsRegisterContract:
    """ADR-001 D5 wiring: fetch + ingest must refuse to run without a valid
    sources/uk_parliament_interests.yml entry (mirrors ec_donations)."""

    def test_ingest_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN the register entry cannot be resolved WHEN ingest_parliament_interests_json
        is called THEN it raises RegisterError and writes nothing to the database."""
        monkeypatch.setattr(parliament_interests_module, "SOURCE_ID", "does_not_exist_xyz")
        import json

        p = tmp_path / "dump.json"
        p.write_text(
            json.dumps(
                [
                    {
                        "id": 1,
                        "category": {"name": "Miscellaneous"},
                        "member": {
                            "id": 1,
                            "nameDisplayAs": "X",
                            "nameListAs": "X",
                            "house": "Commons",
                            "memberFrom": "X",
                            "party": "X",
                        },
                        "fields": [],
                        "registrationDate": "2024-01-01",
                    }
                ]
            )
        )
        with pytest.raises(RegisterError):
            ingest_parliament_interests_json(p)

    def test_fetch_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN the register entry cannot be resolved WHEN fetch_parliament_interests is
        called THEN it raises RegisterError before making any HTTP request."""
        monkeypatch.setattr(parliament_interests_module, "SOURCE_ID", "does_not_exist_xyz")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("fetch_parliament_interests must not make an HTTP request")

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with pytest.raises(RegisterError):
            fetch_parliament_interests(
                tmp_path, page_size=20, polite_delay_seconds=0, client=client
            )
