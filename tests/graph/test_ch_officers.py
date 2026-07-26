"""Tests for the Companies House officers ingest (Phase 1.4).

Verifies the core invariants:
- appointed_on maps to Edge.valid_from, resigned_on to Edge.valid_to
- A still-serving officer (no resigned_on) has Edge.valid_to = None
- date_of_birth / address / nationality are never persisted anywhere,
  including the cached raw JSON on disk (ADR-004 D1 scope boundary)
- An officer with no CH officer ID still gets recorded, at reduced
  match_confidence, never guessed at full confidence
- A missing COMPANIES_HOUSE_API_KEY raises a clear error before any
  network call is attempted
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from uncorrupt.graph.ch_officers import (
    API_KEY_ENV_VAR,
    fetch_company_officers,
    ingest_company_officers,
)
from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.staging.models import Company

RAW_OFFICER_ITEM = {
    "name": "SMITH, John Michael",
    "officer_role": "director",
    "appointed_on": "2015-03-01",
    "resigned_on": "2019-06-30",
    "date_of_birth": {"month": 4, "year": 1970},
    "nationality": "British",
    "address": {"address_line_1": "1 Example Street", "locality": "London"},
    "former_names": [{"forenames": "Jonny", "surname": "Smith"}],
    "occupation": "Company Director",
    "country_of_residence": "England",
    "contact_details": {"contact_name": "Example Corp Secretary"},
    "links": {"officer": {"appointments": "/officers/abc123def456/appointments"}},
}


def _write_cache(tmp_path: Path, company_number: str, items: list[dict]) -> None:
    (tmp_path / f"{company_number}.json").write_text(json.dumps(items))
    (tmp_path / f"{company_number}.provenance.json").write_text(
        json.dumps(
            {
                "company_number": company_number,
                "source_url": f"https://api.company-information.service.gov.uk/company/{company_number}/officers",
                "retrieved_at": "2026-01-01T00:00:00+00:00",
                "content_hash": "sha256:deadbeef",
                "officer_count": len(items),
            }
        )
    )


@pytest.mark.django_db
class TestChOfficersIngest:
    def test_appointed_on_maps_to_valid_from(self, tmp_path):
        """appointed_on becomes Edge.valid_from."""
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])

        ingest_company_officers(["12410514"], tmp_path)

        edge = Attestation.objects.get(source_reference="abc123def456").edge
        assert edge.valid_from.isoformat() == "2015-03-01"

    def test_resigned_on_maps_to_valid_to(self, tmp_path):
        """resigned_on becomes Edge.valid_to."""
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])

        ingest_company_officers(["12410514"], tmp_path)

        edge = Attestation.objects.get(source_reference="abc123def456").edge
        assert edge.valid_to.isoformat() == "2019-06-30"

    def test_still_serving_officer_has_null_valid_to(self, tmp_path):
        """An officer with no resigned_on has Edge.valid_to = None, never inferred."""
        Company.objects.create(company_number="00000010", company_name="Active Co Ltd")
        item = {**RAW_OFFICER_ITEM, "resigned_on": None}
        _write_cache(tmp_path, "00000010", [item])

        ingest_company_officers(["00000010"], tmp_path)

        edge = Attestation.objects.get(source_reference="abc123def456").edge
        assert edge.valid_to is None

    def test_officer_with_no_id_recorded_at_reduced_confidence(self, tmp_path):
        """An officer with no appointments link is still recorded, never at full confidence."""
        Company.objects.create(company_number="00000020", company_name="No ID Co Ltd")
        item = {
            "name": "JONES, Alice",
            "officer_role": "secretary",
            "appointed_on": "2018-01-01",
            "resigned_on": None,
            "date_of_birth": {"month": 5, "year": 1965},
            "nationality": "British",
            "address": {"address_line_1": "2 Example Road"},
            "links": {},
        }
        _write_cache(tmp_path, "00000020", [item])

        summary = ingest_company_officers(["00000020"], tmp_path)

        assert summary["officers_no_id"] == 1
        person = Entity.objects.get(entity_type="person", name="JONES, Alice")
        edge = Edge.objects.get(source_entity=person)
        attestation = edge.attestations.get()
        assert attestation.match_confidence < 1.0
        assert attestation.match_method != "identifier"

    def test_officer_role_stored_in_properties(self, tmp_path):
        """The officer role (director, secretary, etc.) is stored on Edge.properties."""
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])

        ingest_company_officers(["12410514"], tmp_path)

        edge = Attestation.objects.get(source_reference="abc123def456").edge
        assert edge.properties["officer_role"] == "director"

    def test_company_matched_by_number_has_full_confidence(self, tmp_path):
        """A company resolved by company_number yields match_confidence=1.0 for a known officer."""
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])

        ingest_company_officers(["12410514"], tmp_path)

        attestation = Attestation.objects.get(source_reference="abc123def456")
        assert attestation.match_confidence == 1.0
        assert attestation.match_method == "identifier"

    def test_unmatched_company_number_is_counted_and_skipped(self, tmp_path):
        """A company_number not present in staging.Company creates no edges."""
        _write_cache(tmp_path, "99999999", [RAW_OFFICER_ITEM])

        summary = ingest_company_officers(["99999999"], tmp_path)

        assert summary["companies_unmatched"] == 1
        assert summary["edges_created"] == 0

    def test_dob_address_nationality_never_persisted(self, tmp_path):
        """DOB, address, and nationality never end up on any Entity or Edge field."""
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])

        ingest_company_officers(["12410514"], tmp_path)

        person = Entity.objects.get(entity_type="person", registry_id="abc123def456")
        edge = Attestation.objects.get(source_reference="abc123def456").edge
        serialized = json.dumps(
            {"person_properties": person.properties, "edge_properties": edge.properties}
        )
        assert "date_of_birth" not in serialized
        assert "nationality" not in serialized
        assert "address" not in serialized
        assert "1970" not in serialized
        assert "Example Street" not in serialized

    def test_same_named_officer_at_different_companies_is_not_merged(self, tmp_path):
        """Same name, no officer ID, different companies: must be two distinct Entities.

        Merging would attach one company's officer history to an uninvolved
        company at a different one — duplication is the correct outcome
        when identity cannot be proven (governing principle).
        """
        Company.objects.create(company_number="00000030", company_name="Alpha Ltd")
        Company.objects.create(company_number="00000031", company_name="Beta Ltd")
        item = {
            "name": "SMITH, John",
            "officer_role": "director",
            "appointed_on": "2018-01-01",
            "resigned_on": None,
            "links": {},
        }
        _write_cache(tmp_path, "00000030", [item])
        _write_cache(tmp_path, "00000031", [item])

        ingest_company_officers(["00000030", "00000031"], tmp_path)

        people = Entity.objects.filter(entity_type="person", name="SMITH, John")
        assert people.count() == 2
        assert {p.registry_id for p in people} == {
            "00000030:SMITH, JOHN",
            "00000031:SMITH, JOHN",
        }
        for p in people:
            assert p.registry_scheme == "GB-COH-OFFICER-UNRESOLVED"

    def test_unresolved_officer_match_method_is_accurate(self, tmp_path):
        """The match_method for a company-scoped identity must not claim role-based matching."""
        Company.objects.create(company_number="00000032", company_name="Gamma Ltd")
        item = {
            "name": "DOE, Jane",
            "officer_role": "secretary",
            "appointed_on": "2018-01-01",
            "resigned_on": None,
            "links": {},
        }
        _write_cache(tmp_path, "00000032", [item])

        ingest_company_officers(["00000032"], tmp_path)

        edge = Edge.objects.get(source_entity__name="DOE, Jane")
        attestation = edge.attestations.get()
        assert attestation.match_method == "name_company_scoped"

    def test_appointment_self_link_used_as_source_reference(self, tmp_path):
        """Reappointments (same officer_id) at the same company must stay distinct edges."""
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        first_term = {
            **RAW_OFFICER_ITEM,
            "links": {**RAW_OFFICER_ITEM["links"], "self": "/company/12410514/appointments/appt-1"},
        }
        second_term = {
            **RAW_OFFICER_ITEM,
            "appointed_on": "2021-01-01",
            "resigned_on": None,
            "links": {**RAW_OFFICER_ITEM["links"], "self": "/company/12410514/appointments/appt-2"},
        }
        _write_cache(tmp_path, "12410514", [first_term, second_term])

        summary = ingest_company_officers(["12410514"], tmp_path)

        assert summary["edges_created"] == 2
        assert Attestation.objects.filter(
            source_reference="/company/12410514/appointments/appt-1"
        ).exists()
        assert Attestation.objects.filter(
            source_reference="/company/12410514/appointments/appt-2"
        ).exists()

    def test_officer_id_fallback_notes_weaker_claim(self, tmp_path):
        """Without links.self, falling back to the officer ID must be flagged as weaker."""
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])

        ingest_company_officers(["12410514"], tmp_path)

        edge = Attestation.objects.get(source_reference="abc123def456").edge
        assert edge.properties["source_reference_scope"] == "officer_id_not_appointment"

    def test_unparseable_resigned_on_is_not_read_as_still_serving(self, tmp_path):
        """A malformed (non-blank) resigned_on must not silently look like an open appointment."""
        Company.objects.create(company_number="00000033", company_name="Delta Ltd")
        item = {**RAW_OFFICER_ITEM, "resigned_on": "not-a-date"}
        _write_cache(tmp_path, "00000033", [item])

        summary = ingest_company_officers(["00000033"], tmp_path)

        assert summary["unparseable_resigned_on"] == 1
        edge = Edge.objects.get(
            source_entity__name="SMITH, John Michael", target_entity__company_number="00000033"
        )
        assert edge.valid_to is None
        assert edge.properties["resigned_on_unparsed"] == "not-a-date"
        assert edge.properties["resignation_status"] == "ended_date_unknown"

    def test_dropped_personal_fields_never_appear_on_entity_or_edge(self, tmp_path):
        """former_names/occupation/country_of_residence/contact_details never persist."""
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])

        ingest_company_officers(["12410514"], tmp_path)

        person = Entity.objects.get(entity_type="person", registry_id="abc123def456")
        edge = Attestation.objects.get(source_reference="abc123def456").edge
        serialized = json.dumps(
            {"person_properties": person.properties, "edge_properties": edge.properties}
        )
        assert "former_names" not in serialized
        assert "occupation" not in serialized
        assert "country_of_residence" not in serialized
        assert "contact_details" not in serialized
        assert "Jonny" not in serialized


class TestChOfficersFetchStripsPersonalFields:
    def test_fetch_strips_personal_fields_before_caching_to_disk(self, tmp_path, monkeypatch):
        """DOB/address/nationality are stripped before the raw response ever touches disk."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"items": [RAW_OFFICER_ITEM], "total_results": 1})

        client = httpx.Client(transport=httpx.MockTransport(handler))

        fetch_company_officers(["12410514"], tmp_path, client=client)

        cached = json.loads((tmp_path / "12410514.json").read_text())
        assert len(cached) == 1
        assert "date_of_birth" not in cached[0]
        assert "nationality" not in cached[0]
        assert "address" not in cached[0]
        assert "former_names" not in cached[0]
        assert "occupation" not in cached[0]
        assert "country_of_residence" not in cached[0]
        assert "contact_details" not in cached[0]
        assert cached[0]["name"] == "SMITH, John Michael"

    def test_fetch_is_resumable_and_skips_cached_company(self, tmp_path, monkeypatch):
        """A company already cached is skipped on re-run, not re-fetched."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"items": [RAW_OFFICER_ITEM], "total_results": 1})

        client = httpx.Client(transport=httpx.MockTransport(handler))

        fetch_company_officers(["12410514"], tmp_path, client=client)
        assert call_count == 1

        results = fetch_company_officers(["12410514"], tmp_path, client=client)
        assert call_count == 1
        assert results[0].cached is True

    def test_missing_api_key_raises_clear_error(self, tmp_path, monkeypatch):
        """No COMPANIES_HOUSE_API_KEY set raises a clear error, no network call attempted."""
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)

        with pytest.raises(RuntimeError, match=API_KEY_ENV_VAR):
            fetch_company_officers(["12410514"], tmp_path)

    def test_stale_cache_beyond_max_age_is_refetched(self, tmp_path, monkeypatch):
        """A cache entry older than max_cache_age_days is not trusted forever."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"items": [RAW_OFFICER_ITEM], "total_results": 1})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        fetch_company_officers(["12410514"], tmp_path, client=client)
        assert call_count == 1

        stale_provenance = json.loads((tmp_path / "12410514.provenance.json").read_text())
        stale_provenance["retrieved_at"] = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        (tmp_path / "12410514.provenance.json").write_text(json.dumps(stale_provenance))

        results = fetch_company_officers(
            ["12410514"], tmp_path, client=client, max_cache_age_days=30
        )
        assert call_count == 2
        assert results[0].cached is False

    def test_tampered_cache_content_hash_mismatch_is_refetched(self, tmp_path, monkeypatch):
        """A cache file whose content no longer matches its stored hash is not trusted."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"items": [RAW_OFFICER_ITEM], "total_results": 1})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        fetch_company_officers(["12410514"], tmp_path, client=client)
        assert call_count == 1

        (tmp_path / "12410514.json").write_text(json.dumps([{"name": "TAMPERED"}]))

        results = fetch_company_officers(["12410514"], tmp_path, client=client)
        assert call_count == 2
        assert results[0].cached is False

    def test_fresh_valid_cache_is_still_trusted(self, tmp_path, monkeypatch):
        """A fresh, unmodified cache entry is still used without refetching (sanity check)."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(200, json={"items": [RAW_OFFICER_ITEM], "total_results": 1})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        fetch_company_officers(["12410514"], tmp_path, client=client)

        results = fetch_company_officers(["12410514"], tmp_path, client=client)
        assert call_count == 1
        assert results[0].cached is True
