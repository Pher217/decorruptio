"""Tests for the UK Register of Overseas Entities (ROE) ingest.

Verifies the core invariants:
- The connector refuses to run without a `sources/uk_roe.yml` register entry
- Individual beneficial owners and individual managing officers are NEVER
  turned into an Entity/Edge, and their personal fields (name, DOB,
  nationality, address, ...) never reach disk, let alone the database —
  only a count of their existence is recorded (ADR-004 D1)
- Corporate/legal-entity beneficial owners and corporate managing officers
  are fully ingested (no personal data involved)
- Attestation.observed_at/snapshot_ref come from the source's own dates and
  the cached snapshot's content hash — never today's download time
- Re-ingesting the same cache is idempotent (no duplicate entities/edges)
- A company with no cached bundle is counted as unmatched, never invented
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

from uncorrupt.core.errors import RegisterError
from uncorrupt.graph import overseas_entities
from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.graph.overseas_entities import (
    API_KEY_ENV_VAR,
    fetch_overseas_entities,
    fetch_overseas_entity_details,
    ingest_overseas_entities,
)

RAW_INDIVIDUAL_BO_ITEM: dict[str, Any] = {
    "kind": "individual-beneficial-owner",
    "name": "Jane Quinn Example",
    "name_elements": {"forename": "Jane", "surname": "Example"},
    "date_of_birth": {"month": 3, "year": 1975},
    "nationality": "British",
    "address": {"address_line_1": "1 Example Street", "locality": "London"},
    "principal_office_address": {"address_line_1": "Villa 1", "country": "United Arab Emirates"},
    "notified_on": "2023-01-10",
    "ceased": False,
    "natures_of_control": ["ownership-of-shares-more-than-25-percent-registered-overseas-entity"],
    "links": {
        "self": (
            "/company/OE000001/persons-with-significant-control/individual-beneficial-owner/abc123"
        )
    },
}

RAW_CORPORATE_BO_ITEM: dict[str, Any] = {
    "kind": "corporate-entity-beneficial-owner",
    "name": "Example Holdings Ltd",
    "identification": {
        "legal_form": "Limited Company",
        "legal_authority": "Jersey",
        "place_registered": "Jersey Financial Services Commission",
        "registration_number": "9153",
    },
    "address": {"address_line_1": "Standard Bank House", "country": "Jersey"},
    "notified_on": "2023-01-10",
    "ceased": False,
    "is_sanctioned": False,
    "natures_of_control": ["ownership-of-shares-more-than-25-percent-registered-overseas-entity"],
    "links": {
        "self": (
            "/company/OE000001/persons-with-significant-control/"
            "corporate-entity-beneficial-owner/xyz789"
        )
    },
}

RAW_MANAGING_OFFICER_INDIVIDUAL: dict[str, Any] = {
    "name": "HANAFI, Mohammed",
    "officer_role": "managing-officer",
    "appointed_on": "2022-12-15",
    "resigned_on": None,
    "date_of_birth": {"month": 8, "year": 1983},
    "nationality": "Saudi Arabian",
    "country_of_residence": "Saudi Arabia",
    "occupation": "Company Director",
    "person_number": "303351410001",
    "address": {"address_line_1": "Cumberland House", "country": "United Kingdom"},
    "links": {
        "self": "/company/OE000001/appointments/appt-individual",
        "officer": {"appointments": "/officers/officer123/appointments"},
    },
}

RAW_MANAGING_OFFICER_CORPORATE: dict[str, Any] = {
    "name": "EXAMPLE CORPORATE SERVICES LTD",
    "officer_role": "corporate-managing-officer",
    "appointed_on": "2022-12-15",
    "resigned_on": None,
    "links": {
        "self": "/company/OE000001/appointments/appt-corporate",
        "officer": {"appointments": "/officers/officer456/appointments"},
    },
}

PROFILE: dict[str, Any] = {
    "company_name": "EXAMPLE OVERSEAS LTD",
    "company_number": "OE000001",
    "company_status": "registered",
    "type": "registered-overseas-entity",
    "date_of_creation": "2022-12-01",
    "jurisdiction": "united-kingdom",
    "foreign_company_details": {"governed_by": "Jersey", "registration_number": "9153"},
    "registered_office_address": {"country": "Jersey"},
}


def _write_bundle_cache(
    tmp_path: Path,
    company_number: str,
    profile: dict[str, Any],
    psc: list[dict[str, Any]],
    officers: list[dict[str, Any]],
    retrieved_at: str = "2023-06-01T00:00:00+00:00",
) -> str:
    """Write a pre-filtered cache bundle exactly as `fetch_overseas_entity_details` would."""
    bundle = {"profile": profile, "psc": psc, "officers": officers}
    content = json.dumps(bundle, indent=2)
    (tmp_path / f"{company_number}.json").write_text(content)
    content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    (tmp_path / f"{company_number}.provenance.json").write_text(
        json.dumps(
            {
                "company_number": company_number,
                "source_url": f"https://api.company-information.service.gov.uk/company/{company_number}",
                "retrieved_at": retrieved_at,
                "content_hash": f"sha256:{content_hash}",
            }
        )
    )
    return content_hash


class TestSourceRegistryGate:
    def test_fetch_overseas_entities_refuses_without_source_registered(self, tmp_path, monkeypatch):
        """GIVEN no sources/<id>.yml entry for the source id, WHEN fetch runs, THEN it refuses
        before any network call."""
        monkeypatch.setattr(overseas_entities, "SOURCE_ID", "uk_roe_test_missing_entry")

        with pytest.raises(RegisterError, match="uk_roe_test_missing_entry"):
            fetch_overseas_entities(tmp_path)

    def test_fetch_overseas_entity_details_refuses_without_source_registered(
        self, tmp_path, monkeypatch
    ):
        """GIVEN no sources/<id>.yml entry, WHEN detail fetch runs, THEN it refuses before
        requiring an API key."""
        monkeypatch.setattr(overseas_entities, "SOURCE_ID", "uk_roe_test_missing_entry")
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)

        with pytest.raises(RegisterError, match="uk_roe_test_missing_entry"):
            fetch_overseas_entity_details(["OE000001"], tmp_path)

    def test_ingest_overseas_entities_refuses_without_source_registered(
        self, tmp_path, monkeypatch
    ):
        """GIVEN no sources/<id>.yml entry, WHEN ingest runs, THEN it refuses even with an
        empty company list."""
        monkeypatch.setattr(overseas_entities, "SOURCE_ID", "uk_roe_test_missing_entry")

        with pytest.raises(RegisterError, match="uk_roe_test_missing_entry"):
            ingest_overseas_entities([], tmp_path)


class TestFetchOverseasEntitiesEnumeration:
    def test_fetch_paginates_and_writes_provenance(self, tmp_path, monkeypatch):
        """GIVEN a 3-result window paginated 2-at-a-time, WHEN fetched, THEN all 3 are written
        and provenance is untruncated."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            start_index = int(request.url.params["start_index"])
            remaining = max(0, 3 - start_index)
            size = min(int(request.url.params["size"]), remaining)
            items = [
                {"company_number": f"OE00000{i}", "company_name": f"Company {i}"}
                for i in range(start_index, start_index + size)
            ]
            return httpx.Response(200, json={"hits": 3, "items": items})

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = fetch_overseas_entities(tmp_path, size=2, client=client)

        assert result.company_count == 3
        assert result.hits == 3
        assert result.truncated is False
        assert call_count == 2
        written = [json.loads(line) for line in result.jsonl_path.read_text().splitlines()]
        assert [c["company_number"] for c in written] == ["OE000000", "OE000001", "OE000002"]

    def test_fetch_marks_truncated_when_hits_exceed_offset_cap(self, tmp_path, monkeypatch):
        """GIVEN more hits than the offset cap can reach, WHEN fetched, THEN truncated=True
        and hits is the true (larger) count."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        monkeypatch.setattr(overseas_entities, "ADVANCED_SEARCH_OFFSET_CAP", 3)

        def handler(request: httpx.Request) -> httpx.Response:
            start_index = int(request.url.params["start_index"])
            size = int(request.url.params["size"])
            items = [
                {"company_number": f"OE{i:06d}"} for i in range(start_index, start_index + size)
            ]
            return httpx.Response(200, json={"hits": 10, "items": items})

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = fetch_overseas_entities(tmp_path, size=2, client=client)

        assert result.hits == 10
        assert result.company_count == 3
        assert result.truncated is True


class TestFetchEntityDetailsPrivacyFiltering:
    def _mock_client(self, psc_items: list[dict], officer_items: list[dict]) -> httpx.Client:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path.endswith("/persons-with-significant-control"):
                return httpx.Response(
                    200, json={"items": psc_items, "total_results": len(psc_items)}
                )
            if path.endswith("/officers"):
                return httpx.Response(
                    200, json={"items": officer_items, "total_results": len(officer_items)}
                )
            return httpx.Response(200, json=PROFILE)

        return httpx.Client(transport=httpx.MockTransport(handler))

    def test_individual_beneficial_owner_personal_fields_never_reach_disk(
        self, tmp_path, monkeypatch
    ):
        """GIVEN a raw individual-beneficial-owner record with full PII, WHEN fetched, THEN
        the cached file has no name/DOB/nationality/address — only kind + ceased."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        client = self._mock_client(psc_items=[RAW_INDIVIDUAL_BO_ITEM], officer_items=[])

        fetch_overseas_entity_details(["OE000001"], tmp_path, client=client)

        cached = json.loads((tmp_path / "OE000001.json").read_text())
        assert cached["psc"] == [{"kind": "individual-beneficial-owner", "ceased": False}]
        psc_text = json.dumps(cached["psc"])
        assert "Jane" not in psc_text
        assert "1975" not in psc_text
        assert "British" not in psc_text
        assert "Example Street" not in psc_text
        assert "date_of_birth" not in psc_text
        assert "nationality" not in psc_text
        assert "address" not in psc_text
        assert "name_elements" not in psc_text
        assert "principal_office_address" not in psc_text

    def test_individual_managing_officer_personal_fields_never_reach_disk(
        self, tmp_path, monkeypatch
    ):
        """GIVEN a raw individual managing-officer record with full PII, WHEN fetched, THEN
        the cached file has no name/DOB/nationality/occupation/person_number/address —
        only officer_role + resigned_on."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        client = self._mock_client(psc_items=[], officer_items=[RAW_MANAGING_OFFICER_INDIVIDUAL])

        fetch_overseas_entity_details(["OE000001"], tmp_path, client=client)

        cached = json.loads((tmp_path / "OE000001.json").read_text())
        assert cached["officers"] == [{"officer_role": "managing-officer", "resigned_on": None}]
        officers_text = json.dumps(cached["officers"])
        assert "HANAFI" not in officers_text
        assert "1983" not in officers_text
        assert "Saudi" not in officers_text
        assert "Cumberland" not in officers_text
        assert "date_of_birth" not in officers_text
        assert "nationality" not in officers_text
        assert "country_of_residence" not in officers_text
        assert "occupation" not in officers_text
        assert "person_number" not in officers_text
        assert "address" not in officers_text
        assert "links" not in officers_text

    def test_corporate_bo_and_corporate_managing_officer_fields_are_kept(
        self, tmp_path, monkeypatch
    ):
        """GIVEN corporate/legal-entity BO and corporate managing-officer records, WHEN
        fetched, THEN their (non-personal) fields are retained."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        client = self._mock_client(
            psc_items=[RAW_CORPORATE_BO_ITEM], officer_items=[RAW_MANAGING_OFFICER_CORPORATE]
        )

        fetch_overseas_entity_details(["OE000001"], tmp_path, client=client)

        cached = json.loads((tmp_path / "OE000001.json").read_text())
        assert cached["psc"][0]["name"] == "Example Holdings Ltd"
        assert cached["psc"][0]["identification"]["registration_number"] == "9153"
        assert cached["officers"][0]["name"] == "EXAMPLE CORPORATE SERVICES LTD"
        assert cached["officers"][0]["officer_role"] == "corporate-managing-officer"

    def test_missing_api_key_raises_clear_error_before_network_call(self, tmp_path, monkeypatch):
        """GIVEN no COMPANIES_HOUSE_API_KEY set, WHEN detail fetch runs, THEN it raises a
        clear RuntimeError, no network call attempted."""
        monkeypatch.delenv(API_KEY_ENV_VAR, raising=False)

        with pytest.raises(RuntimeError, match=API_KEY_ENV_VAR):
            fetch_overseas_entity_details(["OE000001"], tmp_path)

    def test_fetch_is_resumable_and_skips_cached_company(self, tmp_path, monkeypatch):
        """GIVEN a company already cached, WHEN fetched again, THEN it is skipped, not
        re-fetched."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if request.url.path.endswith(
                "/persons-with-significant-control"
            ) or request.url.path.endswith("/officers"):
                return httpx.Response(200, json={"items": [], "total_results": 0})
            return httpx.Response(200, json=PROFILE)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        first = fetch_overseas_entity_details(["OE000001"], tmp_path, client=client)
        assert first[0].cached is False
        calls_after_first = call_count

        second = fetch_overseas_entity_details(["OE000001"], tmp_path, client=client)
        assert second[0].cached is True
        assert call_count == calls_after_first


@pytest.mark.django_db
class TestIngestOverseasEntities:
    def test_oe_entity_created_with_registry_scheme_and_no_company_number(self, tmp_path):
        """GIVEN a cached bundle for an overseas entity, WHEN ingested, THEN an Entity with
        registry_scheme=GB-ROE exists and company_number stays null."""
        _write_bundle_cache(tmp_path, "OE000001", PROFILE, psc=[], officers=[])

        ingest_overseas_entities(["OE000001"], tmp_path)

        entity = Entity.objects.get(
            entity_type="company", registry_scheme="GB-ROE", registry_id="OE000001"
        )
        assert entity.name == "EXAMPLE OVERSEAS LTD"
        assert entity.company_number is None

    def test_corporate_beneficial_owner_creates_ownership_edge_with_correct_dates(self, tmp_path):
        """GIVEN a corporate beneficial owner with notified_on and no ceased_on, WHEN
        ingested, THEN an ownership Edge is created with valid_from=notified_on and open
        valid_to."""
        _write_bundle_cache(tmp_path, "OE000001", PROFILE, psc=[RAW_CORPORATE_BO_ITEM], officers=[])

        ingest_overseas_entities(["OE000001"], tmp_path)

        bo_entity = Entity.objects.get(
            registry_scheme="GB-ROE-BENEFICIAL-OWNER", registry_id="JERSEY:9153"
        )
        oe_entity = Entity.objects.get(registry_scheme="GB-ROE", registry_id="OE000001")
        edge = Edge.objects.get(
            edge_type="ownership", source_entity=bo_entity, target_entity=oe_entity
        )
        assert edge.valid_from.isoformat() == "2023-01-10"
        assert edge.valid_to is None

    def test_corporate_bo_attestation_observed_at_and_snapshot_ref_from_source(self, tmp_path):
        """GIVEN the same corporate BO record, WHEN ingested, THEN Attestation.observed_at
        is derived from notified_on (not download time) and snapshot_ref matches the
        cache's content hash."""
        content_hash = _write_bundle_cache(
            tmp_path, "OE000001", PROFILE, psc=[RAW_CORPORATE_BO_ITEM], officers=[]
        )

        ingest_overseas_entities(["OE000001"], tmp_path)

        psc_ref = (
            "/company/OE000001/persons-with-significant-control/"
            "corporate-entity-beneficial-owner/xyz789"
        )
        attestation = Attestation.objects.get(source_reference=psc_ref)
        assert attestation.observed_at == datetime(2023, 1, 10, tzinfo=UTC)
        assert attestation.snapshot_ref == content_hash

    def test_individual_beneficial_owner_creates_no_entity_only_counted(self, tmp_path):
        """GIVEN two active and one ceased individual-beneficial-owner stub, WHEN ingested,
        THEN no Entity/Edge is created for any of them and the active count is 2."""
        stubs = [
            {"kind": "individual-beneficial-owner", "ceased": False},
            {"kind": "super-secure-beneficial-owner", "ceased": False},
            {"kind": "individual-beneficial-owner", "ceased": True},
        ]
        _write_bundle_cache(tmp_path, "OE000001", PROFILE, psc=stubs, officers=[])

        summary = ingest_overseas_entities(["OE000001"], tmp_path)

        assert summary["individual_bo_count"] == 2
        assert summary["corporate_bo_edges_created"] == 0
        oe_entity = Entity.objects.get(registry_scheme="GB-ROE", registry_id="OE000001")
        assert oe_entity.properties["individual_beneficial_owner_count"] == 2
        assert (
            Entity.objects.filter(registry_scheme__startswith="GB-ROE-BENEFICIAL-OWNER").count()
            == 0
        )
        assert Edge.objects.filter(edge_type="ownership").count() == 0

    def test_corporate_managing_officer_creates_officer_of_edge(self, tmp_path):
        """GIVEN a corporate managing officer with a stable CH officer ID, WHEN ingested,
        THEN an officer_of Edge is created at full confidence."""
        _write_bundle_cache(
            tmp_path, "OE000001", PROFILE, psc=[], officers=[RAW_MANAGING_OFFICER_CORPORATE]
        )

        ingest_overseas_entities(["OE000001"], tmp_path)

        officer_entity = Entity.objects.get(
            registry_scheme="GB-ROE-MANAGING-OFFICER", registry_id="officer456"
        )
        oe_entity = Entity.objects.get(registry_scheme="GB-ROE", registry_id="OE000001")
        edge = Edge.objects.get(
            edge_type="officer_of", source_entity=officer_entity, target_entity=oe_entity
        )
        attestation = edge.attestations.get()
        assert attestation.match_confidence == 1.0
        assert attestation.match_method == "identifier"

    def test_individual_managing_officer_creates_no_entity_only_counted(self, tmp_path):
        """GIVEN an individual managing-officer stub, WHEN ingested, THEN no Entity/Edge is
        created and the count is recorded on the OE entity."""
        stubs = [{"officer_role": "managing-officer", "resigned_on": None}]
        _write_bundle_cache(tmp_path, "OE000001", PROFILE, psc=[], officers=stubs)

        summary = ingest_overseas_entities(["OE000001"], tmp_path)

        assert summary["individual_managing_officer_count"] == 1
        assert summary["corporate_managing_officer_edges_created"] == 0
        oe_entity = Entity.objects.get(registry_scheme="GB-ROE", registry_id="OE000001")
        assert oe_entity.properties["individual_managing_officer_count"] == 1
        assert (
            Entity.objects.filter(registry_scheme__startswith="GB-ROE-MANAGING-OFFICER").count()
            == 0
        )

    def test_reingest_is_idempotent_no_duplicates(self, tmp_path):
        """GIVEN a cache with one corporate BO and one corporate managing officer, WHEN
        ingested twice, THEN entity/edge/attestation counts stay the same."""
        _write_bundle_cache(
            tmp_path,
            "OE000001",
            PROFILE,
            psc=[RAW_CORPORATE_BO_ITEM],
            officers=[RAW_MANAGING_OFFICER_CORPORATE],
        )

        ingest_overseas_entities(["OE000001"], tmp_path)
        entity_count_1 = Entity.objects.count()
        edge_count_1 = Edge.objects.count()
        attestation_count_1 = Attestation.objects.count()

        ingest_overseas_entities(["OE000001"], tmp_path)

        assert Entity.objects.count() == entity_count_1
        assert Edge.objects.count() == edge_count_1
        assert Attestation.objects.count() == attestation_count_1

    def test_unmatched_company_with_no_cache_is_counted_and_skipped(self, tmp_path):
        """GIVEN a company number with no cached bundle, WHEN ingested, THEN it is counted
        as unmatched and no Entity is invented for it."""
        summary = ingest_overseas_entities(["OE999999"], tmp_path)

        assert summary["companies_unmatched"] == 1
        assert Entity.objects.filter(registry_id="OE999999").count() == 0
