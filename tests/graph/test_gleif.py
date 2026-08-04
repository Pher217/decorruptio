"""Tests for the GLEIF LEI ingest.

Verifies the core invariants:
- A GB record with an unpadded local registration number links to the
  right staging.Company
- A non-GB record creates an Entity with no company_number and is not
  wrongly linked
- Re-ingesting the same LEI updates rather than duplicating
- A record missing the LEI is skipped and counted
- fetch_gleif/ingest_gleif refuse to run without a sources/gleif.yml register entry
"""

import json
from pathlib import Path

import httpx
import pytest

import uncorrupt.graph.gleif as gleif_module
from uncorrupt.core.errors import RegisterError
from uncorrupt.graph.gleif import fetch_gleif, ingest_gleif
from uncorrupt.graph.models import Entity
from uncorrupt.staging.models import Company


def _gb_record(
    lei: str = "984500BG7783FREB4988",
    registered_as: str = "7015428",
    registered_at: str = "RA000585",
) -> dict:
    return {
        "type": "lei-records",
        "id": lei,
        "attributes": {
            "lei": lei,
            "entity": {
                "legalName": {"name": "SOLAS BANC REAL ESTATE LTD", "language": "en"},
                "legalAddress": {"country": "GB", "city": "Winchester"},
                "jurisdiction": "GB",
                "category": "GENERAL",
                "legalForm": {"id": "H0PO"},
                "status": "ACTIVE",
                "registeredAt": {"id": registered_at},
                "registeredAs": registered_as,
            },
            "registration": {"status": "ISSUED"},
        },
    }


def _non_gb_record(lei: str = "5493001KJTIIGC8Y1R12", country: str = "DE") -> dict:
    return {
        "type": "lei-records",
        "id": lei,
        "attributes": {
            "lei": lei,
            "entity": {
                "legalName": {"name": "Beispiel GmbH", "language": "de"},
                "legalAddress": {"country": country, "city": "Berlin"},
                "jurisdiction": country,
                "category": "GENERAL",
                "legalForm": {"id": "XYZ1"},
                "status": "ACTIVE",
                "registeredAt": {"id": "RA000584"},
                "registeredAs": "HRB123456",
            },
            "registration": {"status": "ISSUED"},
        },
    }


def _write_jsonl(tmp_path: Path, records: list[dict]) -> Path:
    jsonl_path = tmp_path / "gleif.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record))
            f.write("\n")
    return jsonl_path


@pytest.mark.django_db
class TestGleifIngest:
    def test_gb_record_with_unpadded_number_links_to_company(self, tmp_path):
        """A GB LEI record's unpadded registeredAs resolves to the zero-padded Company row."""
        Company.objects.create(
            company_number="07015428",
            company_name="Solas Banc Real Estate Ltd",
            normalised_name="SOLAS BANC REAL ESTATE LTD",
        )
        jsonl_path = _write_jsonl(tmp_path, [_gb_record(registered_as="7015428")])

        summary = ingest_gleif(jsonl_path)

        assert summary["created"] == 1
        assert summary["gb_linked"] == 1
        entity = Entity.objects.get(registry_scheme="GLEIF-LEI", registry_id="984500BG7783FREB4988")
        assert entity.company_number == "07015428"

    def test_gb_record_from_non_companies_house_authority_does_not_link(self, tmp_path):
        """A GB record whose number is from another authority (here: FCA, RA000592)
        must not link even though it would pad to a real Companies House number —
        the authority, not the jurisdiction, identifies which register issued it.
        """
        Company.objects.create(
            company_number="00636217",
            company_name="Unrelated Real Company Ltd",
            normalised_name="UNRELATED REAL COMPANY LTD",
        )
        jsonl_path = _write_jsonl(
            tmp_path,
            [_gb_record(registered_as="636217", registered_at="RA000592")],
        )

        summary = ingest_gleif(jsonl_path)

        assert summary["created"] == 1
        assert summary["gb_linked"] == 0
        assert summary["gb_other_authority"] == 1
        entity = Entity.objects.get(registry_scheme="GLEIF-LEI", registry_id="984500BG7783FREB4988")
        assert entity.company_number is None
        assert entity.properties["local_registration_authority"] == "RA000592"
        assert entity.properties["local_registration_number"] == "636217"

    def test_gb_record_from_placeholder_authority_does_not_link(self, tmp_path):
        """A GB record under GLEIF's 'authority not on the list' placeholder
        (RA999999) never links — the authority is unknown, so it cannot be
        assumed to be Companies House.
        """
        Company.objects.create(
            company_number="00636217",
            company_name="Unrelated Real Company Ltd",
            normalised_name="UNRELATED REAL COMPANY LTD",
        )
        jsonl_path = _write_jsonl(
            tmp_path,
            [_gb_record(registered_as="636217", registered_at="RA999999")],
        )

        summary = ingest_gleif(jsonl_path)

        assert summary["gb_linked"] == 0
        entity = Entity.objects.get(registry_scheme="GLEIF-LEI", registry_id="984500BG7783FREB4988")
        assert entity.company_number is None

    def test_gb_record_from_companies_house_scotland_links(self, tmp_path):
        """RA000587 (Companies House Scotland) is a Companies House authority
        and must link, same as RA000585 (England and Wales).
        """
        Company.objects.create(
            company_number="SC286832",
            company_name="Aberdeen Group Plc",
            normalised_name="ABERDEEN GROUP PLC",
        )
        jsonl_path = _write_jsonl(
            tmp_path,
            [_gb_record(registered_as="SC286832", registered_at="RA000587")],
        )

        summary = ingest_gleif(jsonl_path)

        assert summary["gb_linked"] == 1
        entity = Entity.objects.get(registry_scheme="GLEIF-LEI", registry_id="984500BG7783FREB4988")
        assert entity.company_number == "SC286832"

    def test_non_gb_record_has_no_company_number(self, tmp_path):
        """A non-GB record creates an Entity with no company_number — never wrongly linked."""
        Company.objects.create(
            company_number="HRB123456",
            company_name="Some Other Ltd",
            normalised_name="SOME OTHER LTD",
        )
        jsonl_path = _write_jsonl(tmp_path, [_non_gb_record()])

        summary = ingest_gleif(jsonl_path)

        assert summary["created"] == 1
        assert summary["gb_linked"] == 0
        entity = Entity.objects.get(registry_scheme="GLEIF-LEI", registry_id="5493001KJTIIGC8Y1R12")
        assert entity.company_number is None

    def test_reingest_same_lei_updates_not_duplicates(self, tmp_path):
        """Re-running ingest on the same LEI updates the existing Entity, never duplicates."""
        record = _gb_record()
        jsonl_path = _write_jsonl(tmp_path, [record])
        summary_1 = ingest_gleif(jsonl_path)
        assert summary_1["created"] == 1

        updated_record = dict(record)
        updated_record["attributes"] = dict(record["attributes"])
        updated_record["attributes"]["entity"] = dict(record["attributes"]["entity"])
        updated_record["attributes"]["entity"]["status"] = "INACTIVE"
        jsonl_path_2 = _write_jsonl(tmp_path, [updated_record])
        summary_2 = ingest_gleif(jsonl_path_2)

        assert summary_2["created"] == 0
        assert summary_2["updated"] == 1
        assert (
            Entity.objects.filter(
                registry_scheme="GLEIF-LEI", registry_id="984500BG7783FREB4988"
            ).count()
            == 1
        )
        entity = Entity.objects.get(registry_scheme="GLEIF-LEI", registry_id="984500BG7783FREB4988")
        assert entity.properties["status"] == "INACTIVE"

    def test_record_missing_lei_is_skipped_and_counted(self, tmp_path):
        """A record with no LEI is skipped, never invented, and counted."""
        record = _gb_record()
        del record["attributes"]["lei"]
        record["id"] = ""
        jsonl_path = _write_jsonl(tmp_path, [record])

        summary = ingest_gleif(jsonl_path)

        assert summary["skipped_no_lei"] == 1
        assert summary["created"] == 0
        assert Entity.objects.count() == 0


class TestGleifFetch:
    def test_fetch_paginates_and_writes_provenance(self, tmp_path, monkeypatch):
        """Fetch follows cursor links until the limit is reached and writes a provenance record."""
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            links = {}
            if call_count < 3:
                links["next"] = (
                    f"https://api.gleif.org/api/v1/lei-records?page%5Bcursor%5D=next{call_count}"
                )
            return httpx.Response(
                200,
                json={
                    "meta": {"pagination": {"total": 3}},
                    "links": links,
                    "data": [_gb_record(lei=f"LEI{call_count}")],
                },
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = fetch_gleif(tmp_path, country="GB", limit=3, page_size=1, client=client)

        assert result.record_count == 3
        assert call_count == 3
        provenance = json.loads(result.provenance_path.read_text())
        assert provenance["record_count"] == 3
        assert provenance["country"] == "GB"

    def test_fetch_backs_off_on_429_then_succeeds(self, tmp_path, monkeypatch):
        """A 429 response triggers a retry rather than an immediate failure."""
        import uncorrupt.graph.gleif as gleif_module

        monkeypatch.setattr(gleif_module.time, "sleep", lambda _seconds: None)
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429)
            return httpx.Response(
                200,
                json={"meta": {"pagination": {"total": 1}}, "data": [_gb_record()]},
            )

        client = httpx.Client(transport=httpx.MockTransport(handler))

        result = fetch_gleif(
            tmp_path,
            country="GB",
            limit=1,
            page_size=1,
            client=client,
            polite_delay_seconds=0,
        )

        assert result.record_count == 1
        assert attempts == 2


class TestGleifRegisterContract:
    def test_ingest_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/gleif.yml cannot be resolved (its source_id is absent from the
        register) WHEN ingest_gleif is called THEN it raises RegisterError and writes
        nothing to the database."""
        monkeypatch.setattr(gleif_module, "SOURCE_ID", "does_not_exist_xyz")
        jsonl_path = _write_jsonl(tmp_path, [_gb_record()])

        with pytest.raises(RegisterError):
            ingest_gleif(jsonl_path)

    def test_fetch_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/gleif.yml cannot be resolved WHEN fetch_gleif is called THEN it
        raises RegisterError before making any HTTP request."""
        monkeypatch.setattr(gleif_module, "SOURCE_ID", "does_not_exist_xyz")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("fetch_gleif must not make an HTTP request")

        client = httpx.Client(transport=httpx.MockTransport(handler))

        with pytest.raises(RegisterError):
            fetch_gleif(tmp_path, country="GB", limit=1, client=client)
