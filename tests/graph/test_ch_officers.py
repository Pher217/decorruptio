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

Also covers the officer-coverage-expansion primitives (`select_next_pending`,
`salted_hash_order`, `procurement_supplier_universe`, `officer_ids_for_companies`,
`coverage_report`, `procurement_universe_coverage_report`, `append_run_manifest`)
and the CLI's salt-persistence helper.

- fetch_company_officers/ingest_company_officers refuse to run without a
  sources/uk_companies_house_officers.yml register entry
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from scripts.ingest_ch_officers import _load_or_create_salt

import uncorrupt.graph.ch_officers as ch_officers_module
from uncorrupt.core.errors import RegisterError
from uncorrupt.graph.ch_officers import (
    API_KEY_ENV_VAR,
    append_run_manifest,
    coverage_report,
    fetch_company_officers,
    ingest_company_officers,
    officer_ids_for_companies,
    procurement_supplier_universe,
    procurement_universe_coverage_report,
    salted_hash_order,
    select_next_pending,
)
from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.staging.models import Award, AwardResolution, Company, Tender

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

    def test_unpadded_company_number_still_resolves(self, tmp_path):
        """An unpadded company number still resolves to the zero-padded Company row
        (as CH stores it) — the padding-bug regression test."""
        Company.objects.create(company_number="07015428", company_name="Example Ltd")
        _write_cache(tmp_path, "7015428", [RAW_OFFICER_ITEM])

        summary = ingest_company_officers(["7015428"], tmp_path)

        assert summary["companies_unmatched"] == 0
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


@pytest.mark.django_db
class TestIngestCompanyOfficersAmbiguousCompanyNumber:
    def test_company_number_with_three_registry_scheme_entities_resolves_to_coh(self, tmp_path):
        """GIVEN a company_number with THREE Entity rows across different registry
        schemes (SC214564's real shape in the live graph: one GB-COH row plus two
        GLEIF-LEI rows -- GLEIF publishes more than one LEI record for some UK
        companies) WHEN ingesting THEN resolution lands on the GB-COH entity without
        raising MultipleObjectsReturned, and neither GLEIF entity is merged or altered
        (ADR-006: duplicate over merge).

        Regression test: `Entity.objects.get_or_create(entity_type="company",
        company_number=...)` without registry_scheme matches on company_number alone
        and raises MultipleObjectsReturned as soon as 2+ Entity rows share it.
        """
        Company.objects.create(company_number="SC214564", company_name="Example Scottish Co Ltd")
        Entity.objects.create(
            entity_type="company",
            registry_scheme="GB-COH",
            registry_id="SC214564",
            name="Example Scottish Co Ltd",
            company_number="SC214564",
        )
        Entity.objects.create(
            entity_type="company",
            registry_scheme="GLEIF-LEI",
            registry_id="529900AAAAAAAAAAAA01",
            name="Example Scottish Co Ltd",
            company_number="SC214564",
        )
        Entity.objects.create(
            entity_type="company",
            registry_scheme="GLEIF-LEI",
            registry_id="529900BBBBBBBBBBBB02",
            name="Example Scottish Co Ltd (Renamed)",
            company_number="SC214564",
        )
        _write_cache(tmp_path, "SC214564", [RAW_OFFICER_ITEM])

        summary = ingest_company_officers(["SC214564"], tmp_path)

        assert summary["ambiguous_company_number"] == 0
        coh_entity = Entity.objects.get(registry_scheme="GB-COH", registry_id="SC214564")
        edge = Attestation.objects.get(source_reference="abc123def456").edge
        assert edge.target_entity_id == coh_entity.id
        # Both GLEIF entities must still exist, untouched — never merged
        assert (
            Entity.objects.filter(registry_scheme="GLEIF-LEI", company_number="SC214564").count()
            == 2
        )

    def test_ambiguous_company_number_counter_increments_run_continues(self, tmp_path, monkeypatch):
        """GIVEN company entity resolution raises MultipleObjectsReturned for one of two
        companies WHEN ingest_company_officers runs THEN that company is counted under
        `ambiguous_company_number` and skipped, while the other company's officers are
        still ingested normally -- one bad row must not crash or lose the whole run.
        """
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        Company.objects.create(company_number="00000099", company_name="Clean Co Ltd")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])
        clean_item = {
            **RAW_OFFICER_ITEM,
            "links": {"officer": {"appointments": "/officers/xyz789/appointments"}},
        }
        _write_cache(tmp_path, "00000099", [clean_item])

        real_canonical = ch_officers_module._canonical_company_entity

        def _raise_for_one_company(company):
            if company.company_number == "12410514":
                raise Entity.MultipleObjectsReturned("simulated ambiguity")
            return real_canonical(company)

        monkeypatch.setattr(ch_officers_module, "_canonical_company_entity", _raise_for_one_company)

        summary = ingest_company_officers(["12410514", "00000099"], tmp_path)

        assert summary["ambiguous_company_number"] == 1
        assert summary["companies_processed"] == 1
        assert Attestation.objects.filter(source_reference="xyz789").exists()
        assert not Attestation.objects.filter(source_reference="abc123def456").exists()


@pytest.mark.django_db
class TestIngestCompanyOfficersSelectionRule:
    def test_selection_rule_is_stamped_on_newly_created_edge(self, tmp_path):
        """GIVEN a selection_rule WHEN a new officer_of edge is created THEN the edge's
        properties record that selection_rule -- provenance of why this edge was ever
        fetched, independent of any live universe-membership re-check."""
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])

        ingest_company_officers(
            ["12410514"], tmp_path, selection_rule="universe=procurement-suppliers"
        )

        edge = Attestation.objects.get(source_reference="abc123def456").edge
        assert edge.properties["selection_rule"] == "universe=procurement-suppliers"

    def test_selection_rule_is_not_overwritten_on_re_ingest_under_a_different_rule(self, tmp_path):
        """GIVEN an edge already created under one selection_rule WHEN re-ingested
        (get_or_create matches the existing edge) under a DIFFERENT selection_rule THEN
        the original rule is preserved -- get_or_create only applies `defaults` at
        creation, so a later fetch under a new rule cannot rewrite provenance for an
        edge that already existed."""
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])
        ingest_company_officers(["12410514"], tmp_path, selection_rule="first-rule")

        ingest_company_officers(["12410514"], tmp_path, selection_rule="second-rule")

        edge = Attestation.objects.get(source_reference="abc123def456").edge
        assert edge.properties["selection_rule"] == "first-rule"

    def test_no_selection_rule_leaves_properties_without_the_key(self, tmp_path):
        """GIVEN no selection_rule argument (existing call sites, unchanged) WHEN
        ingesting THEN the edge's properties contain no selection_rule key at all."""
        Company.objects.create(company_number="12410514", company_name="PPE Medpro Ltd")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])

        ingest_company_officers(["12410514"], tmp_path)

        edge = Attestation.objects.get(source_reference="abc123def456").edge
        assert "selection_rule" not in edge.properties


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


class TestFetchCompanyOfficersPerCompanyFailure:
    def test_one_company_failing_does_not_abort_the_batch(self, tmp_path, monkeypatch):
        """GIVEN one company whose fetch raises a non-retryable HTTP error WHEN fetching
        a batch of two THEN the OTHER company is still fetched successfully -- a single
        bad company must not kill a multi-hour sweep."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")

        def handler(request: httpx.Request) -> httpx.Response:
            if "00000001" in str(request.url):
                return httpx.Response(403)
            return httpx.Response(200, json={"items": [RAW_OFFICER_ITEM], "total_results": 1})

        client = httpx.Client(transport=httpx.MockTransport(handler))

        results = fetch_company_officers(
            ["00000001", "00000002"], tmp_path, client=client, polite_delay_seconds=0
        )

        assert [r.company_number for r in results] == ["00000002"]

    def test_failed_company_writes_no_cache_file(self, tmp_path, monkeypatch):
        """GIVEN a company whose fetch fails WHEN fetching THEN no cache file is written
        for it, so `select_next_pending` still treats it as pending on the next
        invocation rather than silently marking it done."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        fetch_company_officers(["00000001"], tmp_path, client=client, polite_delay_seconds=0)

        assert not (tmp_path / "00000001.json").exists()
        assert not (tmp_path / "00000001.provenance.json").exists()


class TestFetchCompanyOfficersCircuitBreaker:
    def test_aborts_sweep_after_max_consecutive_failures(self, tmp_path, monkeypatch):
        """GIVEN every company fails WHEN max_consecutive_failures=3 over a batch of 10
        THEN the sweep aborts after exactly 3 consecutive failures -- the remaining 7
        companies are never attempted (no HTTP request made for them)."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(403)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        company_numbers = [f"0000000{i}" for i in range(10)]

        results = fetch_company_officers(
            company_numbers,
            tmp_path,
            client=client,
            polite_delay_seconds=0,
            max_consecutive_failures=3,
        )

        assert results == []
        assert call_count == 3

    def test_a_success_resets_the_circuit_so_the_sweep_completes(self, tmp_path, monkeypatch):
        """GIVEN failures that alternate with successes (never 2 in a row) WHEN
        max_consecutive_failures=2 THEN the circuit never trips and every company is
        attempted -- a success resets the streak rather than the threshold being a
        cumulative failure count."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        successful = {"00000002", "00000004"}

        def handler(request: httpx.Request) -> httpx.Response:
            if any(n in str(request.url) for n in successful):
                return httpx.Response(200, json={"items": [RAW_OFFICER_ITEM], "total_results": 1})
            return httpx.Response(403)

        client = httpx.Client(transport=httpx.MockTransport(handler))

        results = fetch_company_officers(
            ["00000001", "00000002", "00000003", "00000004", "00000005"],
            tmp_path,
            client=client,
            polite_delay_seconds=0,
            max_consecutive_failures=2,
        )

        assert [r.company_number for r in results] == ["00000002", "00000004"]


class TestChOfficersRegisterContract:
    def test_ingest_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/uk_companies_house_officers.yml cannot be resolved (its
        source_id is absent from the register) WHEN ingest_company_officers is called
        THEN it raises RegisterError and writes nothing to the database."""
        monkeypatch.setattr(ch_officers_module, "SOURCE_ID", "does_not_exist_xyz")
        _write_cache(tmp_path, "12410514", [RAW_OFFICER_ITEM])

        with pytest.raises(RegisterError):
            ingest_company_officers(["12410514"], tmp_path)

    def test_fetch_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/uk_companies_house_officers.yml cannot be resolved WHEN
        fetch_company_officers is called THEN it raises RegisterError before making
        any HTTP request."""
        monkeypatch.setattr(ch_officers_module, "SOURCE_ID", "does_not_exist_xyz")

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("fetch_company_officers must not make an HTTP request")

        client = httpx.Client(transport=httpx.MockTransport(handler))

        with pytest.raises(RegisterError):
            fetch_company_officers(["12410514"], tmp_path, client=client)


def _write_valid_cache(
    tmp_path: Path, company_number: str, items: list[dict], retrieved_at: str | None = None
) -> None:
    """Write a real, hash-verified cache entry.

    Unlike `_write_cache` above (whose fixed 'sha256:deadbeef' content_hash
    never matches -- fine for ingest-only tests that read the JSON directly),
    this computes the real hash so cache-validity checks (`_is_freshly_cached`
    / `select_next_pending`) actually see it as fresh.
    """
    payload = json.dumps(items)
    (tmp_path / f"{company_number}.json").write_text(payload)
    content_hash = f"sha256:{hashlib.sha256(payload.encode()).hexdigest()}"
    (tmp_path / f"{company_number}.provenance.json").write_text(
        json.dumps(
            {
                "company_number": company_number,
                "source_url": f"https://api.company-information.service.gov.uk/company/{company_number}/officers",
                "retrieved_at": retrieved_at or datetime.now(UTC).isoformat(),
                "content_hash": content_hash,
                "officer_count": len(items),
            }
        )
    )


class TestSelectNextPending:
    def test_limit_none_returns_all_companies_unchanged(self, tmp_path):
        """GIVEN limit=None WHEN selecting THEN every company is returned, in the given order."""
        result = select_next_pending(["3", "1", "2"], tmp_path, limit=None)
        assert result == ["3", "1", "2"]

    def test_skips_companies_with_valid_cache(self, tmp_path):
        """GIVEN one company already validly cached WHEN limit=2 THEN only the uncached
        companies are selected, cached ones are skipped entirely."""
        _write_valid_cache(tmp_path, "00000001", [RAW_OFFICER_ITEM])

        result = select_next_pending(["00000001", "00000002", "00000003"], tmp_path, limit=2)

        assert result == ["00000002", "00000003"]

    def test_stops_at_limit_after_skipping_cached(self, tmp_path):
        """GIVEN a cached company ahead of two pending ones WHEN limit=1 THEN exactly one
        pending company is returned, not the cached one and not both pending ones."""
        _write_valid_cache(tmp_path, "00000001", [RAW_OFFICER_ITEM])

        result = select_next_pending(["00000001", "00000002", "00000003"], tmp_path, limit=1)

        assert result == ["00000002"]

    def test_treats_stale_cache_as_pending(self, tmp_path):
        """GIVEN a cache entry older than max_cache_age_days WHEN selecting THEN it is
        treated as pending (selected), not skipped as fresh."""
        stale_retrieved_at = (datetime.now(UTC) - timedelta(days=31)).isoformat()
        _write_valid_cache(
            tmp_path, "00000001", [RAW_OFFICER_ITEM], retrieved_at=stale_retrieved_at
        )

        result = select_next_pending(["00000001"], tmp_path, limit=5, max_cache_age_days=30)

        assert result == ["00000001"]

    def test_treats_corrupted_provenance_as_pending(self, tmp_path):
        """GIVEN a provenance file that is not valid JSON WHEN selecting THEN the company
        is treated as pending rather than raising."""
        (tmp_path / "00000001.json").write_text(json.dumps([RAW_OFFICER_ITEM]))
        (tmp_path / "00000001.provenance.json").write_text("{not valid json")

        result = select_next_pending(["00000001"], tmp_path, limit=5)

        assert result == ["00000001"]

    def test_limit_zero_selects_nothing(self, tmp_path):
        """GIVEN limit=0 WHEN selecting THEN no companies are selected at all."""
        result = select_next_pending(["00000001", "00000002"], tmp_path, limit=0)
        assert result == []


class TestSaltedHashOrder:
    def test_order_matches_independent_sha256_computation(self):
        """GIVEN a fixed company list and salt WHEN ordered THEN the result matches the
        exact sha256(salt+company_number)-ascending order computed independently
        (not company-number-ascending -- this salt/set combination reverses it)."""
        numbers = ["00000001", "00000002", "00000003", "00000004"]

        result = salted_hash_order(numbers, salt="fixed-salt-123")

        assert result == ["00000004", "00000003", "00000002", "00000001"]

    def test_same_salt_is_deterministic_across_calls(self):
        """GIVEN the same salt WHEN called twice THEN the order is identical both times."""
        numbers = ["00000005", "00000012", "00000099", "00000003"]

        first = salted_hash_order(numbers, salt="repeat-salt")
        second = salted_hash_order(numbers, salt="repeat-salt")

        assert first == second

    def test_preserves_the_same_set_of_companies(self):
        """GIVEN a company list WHEN reordered THEN no company is added, dropped, or
        duplicated -- only the order changes."""
        numbers = ["00000007", "00000002", "00000009"]

        result = salted_hash_order(numbers, salt="some-salt")

        assert sorted(result) == sorted(numbers)


def _make_award_resolution(
    award_id: str,
    company=None,
    company_number: str | None = None,
    match_confidence: float = 0.0,
    match_method: str | None = None,
    normalisation_note: str | None = None,
) -> AwardResolution:
    """Create an Award + AwardResolution -- the ADR-012 D1 per-award grain."""
    tender = Tender.objects.create(
        source_id="uk_contracts_finder",
        tender_id=f"tender-{award_id}",
        source_url="https://example.com",
    )
    award = Award.objects.create(
        source_id="uk_contracts_finder",
        tender_id=tender.tender_id,
        tender_ref=tender,
        award_id=award_id,
        supplier_name="Test Supplier Ltd",
        currency="GBP",
        value_amount_cents=5000000,
        status="active",
        raw_json={},
    )
    return AwardResolution.objects.create(
        award=award,
        source_id="uk_contracts_finder",
        company=company,
        company_number=company_number,
        match_confidence=match_confidence,
        match_method=match_method,
        normalisation_note=normalisation_note,
    )


@pytest.mark.django_db
class TestProcurementSupplierUniverse:
    def test_returns_distinct_resolved_company_numbers(self):
        """GIVEN two verified award resolutions pointing at the same company WHEN
        building the universe THEN the company number appears exactly once."""
        company = Company.objects.create(company_number="00000001", company_name="Acme Ltd")
        _make_award_resolution(
            "award-1",
            company=company,
            company_number="00000001",
            match_confidence=1.0,
            match_method="identifier",
        )
        _make_award_resolution(
            "award-2",
            company=company,
            company_number="00000001",
            match_confidence=0.9,
            match_method="exact_name",
        )

        universe = procurement_supplier_universe()

        assert universe == ["00000001"]

    def test_excludes_unresolved_suppliers_with_no_company_number(self):
        """GIVEN an award resolution with no resolved company_number WHEN building the
        universe THEN that row contributes nothing to it."""
        _make_award_resolution("award-1", company=None, company_number=None)

        universe = procurement_supplier_universe()

        assert universe == []

    def test_excludes_unverified_identifier_that_carries_a_company_number(self):
        """GIVEN a GB-COH identifier match that FAILED against the CH bulk snapshot --
        `resolve_suppliers` still sets `company_number=sid` (the normalised, unverified
        external identifier) even though `company=None` and `match_confidence=0.0`
        (see `staging/companies_house.py`'s "not found in CH bulk snapshot" branch) --
        WHEN building the universe THEN that row is excluded. Filtering on
        `company_number__isnull=False` alone would wrongly admit it."""
        _make_award_resolution(
            "award-1",
            company=None,
            company_number="09999999",
            match_confidence=0.0,
            match_method=None,
            normalisation_note="GB-COH identifier '09999999' not found in CH bulk snapshot.",
        )

        universe = procurement_supplier_universe()

        assert universe == []

    def test_empty_when_no_supplier_resolutions_exist(self):
        """GIVEN no AwardResolution rows at all WHEN building the universe THEN it is
        empty, never invented."""
        assert procurement_supplier_universe() == []


@pytest.mark.django_db
class TestOfficerIdsForCompanies:
    def test_returns_registry_id_of_officer_with_stable_id(self):
        """GIVEN a company with one identifier-matched officer WHEN queried THEN that
        officer's registry_id is returned."""
        company = _make_company_entity("00000001")
        officer = _make_officer_entity("officer-abc")
        _make_officer_of_edge(officer, company, source_url="https://x/company/00000001/officers")

        result = officer_ids_for_companies(["00000001"])

        assert result == ["officer-abc"]

    def test_excludes_officer_with_no_stable_id(self):
        """GIVEN a company whose only officer has no stable CH officer ID
        (GB-COH-OFFICER-UNRESOLVED) WHEN queried THEN nothing is returned -- expanding an
        unresolved placeholder would risk fetching the wrong person's appointments."""
        company = _make_company_entity("00000002")
        unresolved = Entity.objects.create(
            entity_type="person",
            registry_scheme="GB-COH-OFFICER-UNRESOLVED",
            registry_id="00000002:JONES, ALICE",
            name="JONES, Alice",
        )
        _make_officer_of_edge(unresolved, company, source_url="https://x/company/00000002/officers")

        result = officer_ids_for_companies(["00000002"])

        assert result == []

    def test_same_officer_across_two_companies_is_not_duplicated(self):
        """GIVEN one officer serving on two companies in the batch WHEN queried THEN the
        officer's registry_id appears exactly once."""
        officer = _make_officer_entity("officer-shared")
        company_a = _make_company_entity("00000003")
        company_b = _make_company_entity("00000004")
        _make_officer_of_edge(officer, company_a, source_url="https://x/company/00000003/officers")
        _make_officer_of_edge(officer, company_b, source_url="https://x/company/00000004/officers")

        result = officer_ids_for_companies(["00000003", "00000004"])

        assert result == ["officer-shared"]

    def test_unpadded_company_number_still_resolves(self):
        """GIVEN a company stored zero-padded WHEN queried with an unpadded number THEN
        it still resolves (mirrors the ingest-side padding normalisation)."""
        company = _make_company_entity("00000005")
        officer = _make_officer_entity("officer-padded")
        _make_officer_of_edge(officer, company, source_url="https://x/company/00000005/officers")

        result = officer_ids_for_companies(["5"])

        assert result == ["officer-padded"]


@pytest.mark.django_db
class TestCoverageReport:
    def test_direct_roster_fetch_company_is_counted_in_that_tier(self):
        """GIVEN a company with an officer_of edge attested by a direct /officers fetch
        WHEN reporting coverage THEN it counts as direct_roster_fetch, not zero_officers."""
        company = _make_company_entity("00000001")
        officer = _make_officer_entity("officer-1")
        _make_officer_of_edge(officer, company, source_url="https://x/company/00000001/officers")

        report = coverage_report()

        assert report["direct_roster_fetch"] == 1

    def test_appointment_hop_only_company_is_not_counted_as_direct_fetch(self):
        """GIVEN a company whose only officer_of edge came from an /appointments walk
        WHEN reporting coverage THEN it counts as appointment_hop_only, not
        direct_roster_fetch -- it never had its own roster fetched."""
        company = _make_company_entity("00000002")
        officer = _make_officer_entity("officer-2")
        _make_officer_of_edge(
            officer, company, source_url="https://x/officers/officer-2/appointments"
        )

        report = coverage_report()

        assert report["appointment_hop_only"] == 1
        assert report["direct_roster_fetch"] == 0

    def test_company_with_no_officer_edge_is_counted_as_zero_officers(self):
        """GIVEN a company with no officer_of edge at all WHEN reporting coverage THEN it
        counts as zero_officers."""
        _make_company_entity("00000003")

        report = coverage_report()

        assert report["zero_officers"] == 1

    def test_tier_counts_sum_to_total(self):
        """GIVEN a mix of direct-fetch, appointment-hop-only, and zero-officer companies
        WHEN reporting coverage THEN the three tiers sum exactly to the total."""
        direct = _make_company_entity("00000004")
        hop_only = _make_company_entity("00000005")
        _make_company_entity("00000006")  # zero officers
        _make_officer_of_edge(
            _make_officer_entity("officer-4"),
            direct,
            source_url="https://x/company/00000004/officers",
        )
        _make_officer_of_edge(
            _make_officer_entity("officer-5"),
            hop_only,
            source_url="https://x/officers/officer-5/appointments",
        )

        report = coverage_report()

        assert (
            report["direct_roster_fetch"] + report["appointment_hop_only"] + report["zero_officers"]
            == report["total_gb_coh_companies"]
        )

    def test_direct_roster_fetch_split_by_universe_membership(self):
        """GIVEN one direct-roster-fetch company inside the procurement-supplier
        universe and one outside it WHEN reporting coverage THEN
        direct_roster_fetch_by_universe_membership attributes each to the right side --
        this is the split that tells apart the old (potentially benchmark-tainted)
        seed from today's clean universe, rather than blending them."""
        in_universe = _make_company_entity("00000007")
        _make_verified_supplier_resolution("00000007", "In Universe Ltd")
        outside_universe = _make_company_entity("00000008")
        _make_officer_of_edge(
            _make_officer_entity("officer-7"),
            in_universe,
            source_url="https://x/company/00000007/officers",
        )
        _make_officer_of_edge(
            _make_officer_entity("officer-8"),
            outside_universe,
            source_url="https://x/company/00000008/officers",
        )

        report = coverage_report()
        breakdown = report["direct_roster_fetch_by_universe_membership"]

        assert breakdown["in_procurement_universe"] == 1
        assert breakdown["outside_procurement_universe"] == 1


@pytest.mark.django_db
class TestProcurementUniverseCoverageReport:
    def test_universe_size_matches_distinct_resolved_suppliers(self):
        """GIVEN two verified AwardResolution rows resolving to two distinct
        companies WHEN reporting universe coverage THEN universe_size is exactly 2."""
        _make_verified_supplier_resolution("00000001", "Acme Ltd")
        _make_verified_supplier_resolution("00000002", "Beta Ltd")

        report = procurement_universe_coverage_report()

        assert report["universe_size"] == 2

    def test_company_outside_universe_is_excluded_from_report(self):
        """GIVEN a GB-COH company entity with officers but NOT referenced by any
        verified AwardResolution WHEN reporting universe coverage THEN it is not
        counted anywhere in the universe report."""
        _make_verified_supplier_resolution("00000001", "Acme Ltd")
        outside_universe = _make_company_entity("00000099")
        _make_officer_of_edge(
            _make_officer_entity("officer-99"),
            outside_universe,
            source_url="https://x/company/00000099/officers",
        )

        report = procurement_universe_coverage_report()

        assert report["universe_size"] == 1
        assert report["universe_with_graph_entity"] == 0

    def test_universe_company_with_no_graph_entity_counts_as_zero_officers(self):
        """GIVEN a verified resolved supplier company that has never been touched by
        the graph pipeline (no Entity created at all) WHEN reporting universe coverage
        THEN it still counts towards zero_officers, and not towards
        universe_with_graph_entity."""
        _make_verified_supplier_resolution("00000042", "Never Touched Ltd")

        report = procurement_universe_coverage_report()

        assert report["universe_with_graph_entity"] == 0
        assert report["zero_officers"] == 1

    def test_universe_tier_counts_sum_to_universe_size(self):
        """GIVEN a mix of universe companies (with an entity + officers, with an entity
        but no officers, and with no entity at all) WHEN reporting universe coverage
        THEN the tiers sum exactly to universe_size."""
        for i, number in enumerate(["00000001", "00000002", "00000003"]):
            _make_verified_supplier_resolution(number, f"Supplier {i}")
        with_officer = _make_company_entity("00000001")
        _make_company_entity("00000002")  # entity exists, no officers
        # 00000003: no Entity at all
        _make_officer_of_edge(
            _make_officer_entity("officer-1"),
            with_officer,
            source_url="https://x/company/00000001/officers",
        )

        report = procurement_universe_coverage_report()

        assert (
            report["direct_roster_fetch"] + report["appointment_hop_only"] + report["zero_officers"]
            == report["universe_size"]
        )


class TestAppendRunManifest:
    def test_appends_one_json_line_per_call(self, tmp_path):
        """GIVEN two separate calls WHEN appending THEN the manifest has exactly two
        lines, not one overwritten line."""
        append_run_manifest(tmp_path, selection_rule="first run", fetched=1)
        append_run_manifest(tmp_path, selection_rule="second run", fetched=2)

        lines = (tmp_path / "run_manifest.jsonl").read_text().splitlines()

        assert len(lines) == 2

    def test_record_includes_given_fields_and_a_timestamp(self, tmp_path):
        """GIVEN a call with arbitrary keyword fields WHEN appending THEN the resulting
        JSON record contains those exact fields plus a recorded_at timestamp."""
        append_run_manifest(tmp_path, selection_rule="universe=procurement-suppliers", salt="abc")

        record = json.loads((tmp_path / "run_manifest.jsonl").read_text().splitlines()[0])

        assert record["selection_rule"] == "universe=procurement-suppliers"
        assert record["salt"] == "abc"
        assert "recorded_at" in record


def _make_company_entity(company_number: str, name: str = "Test Co Ltd") -> Entity:
    return Entity.objects.create(
        entity_type="company",
        company_number=company_number,
        registry_scheme="GB-COH",
        registry_id=company_number,
        name=name,
    )


def _make_officer_entity(registry_id: str, name: str = "SMITH, John") -> Entity:
    return Entity.objects.create(
        entity_type="person",
        registry_scheme="GB-COH-OFFICER",
        registry_id=registry_id,
        name=name,
    )


def _make_officer_of_edge(
    person: Entity, company: Entity, source_url: str, source_reference: str = "ref"
) -> Edge:
    edge = Edge.objects.create(edge_type="officer_of", source_entity=person, target_entity=company)
    Attestation.objects.create(
        edge=edge,
        source_name="Companies House",
        source_url=source_url,
        source_reference=source_reference,
        match_confidence=1.0,
        match_method="identifier",
    )
    return edge


def _make_verified_supplier_resolution(
    company_number: str, supplier_name: str = "Test Supplier Ltd"
) -> AwardResolution:
    """Create an AwardResolution that is actually verified (`company` FK set).

    Mirrors the real success path in `resolve_suppliers` -- NOT the "GB-COH
    identifier present but never matched against the CH bulk snapshot"
    failure path, which sets `company_number=sid` while leaving `company`
    None and `match_confidence=0.0` (see
    `test_excludes_unverified_identifier_that_carries_a_company_number`).
    """
    company, _ = Company.objects.get_or_create(
        company_number=company_number, defaults={"company_name": supplier_name}
    )
    return _make_award_resolution(
        f"award-{company_number}",
        company=company,
        company_number=company_number,
        match_confidence=1.0,
        match_method="identifier",
    )


class TestLoadOrCreateSalt:
    def test_explicit_salt_always_wins(self, tmp_path):
        """GIVEN an explicit salt WHEN loading THEN it is returned unchanged, even if a
        manifest with a different salt already exists."""
        append_run_manifest(tmp_path, selection_rule="prior run", salt="salt-from-manifest")

        result = _load_or_create_salt(tmp_path, explicit_salt="salt-from-cli")

        assert result == "salt-from-cli"

    def test_reuses_salt_from_existing_manifest(self, tmp_path):
        """GIVEN no explicit salt but an existing manifest recording one WHEN loading
        THEN the manifest's salt is reused, not a freshly generated one."""
        append_run_manifest(tmp_path, selection_rule="first run", salt="pinned-salt")

        result = _load_or_create_salt(tmp_path, explicit_salt=None)

        assert result == "pinned-salt"

    def test_generates_a_new_salt_when_no_manifest_exists(self, tmp_path):
        """GIVEN no explicit salt and no manifest file WHEN loading THEN a new 32-character
        hex salt is generated (secrets.token_hex(16))."""
        result = _load_or_create_salt(tmp_path, explicit_salt=None)

        assert len(result) == 32
        int(result, 16)  # raises ValueError if not valid hex

    def test_reuses_earliest_recorded_salt_across_multiple_manifest_lines(self, tmp_path):
        """GIVEN a manifest with two runs recording different salts WHEN loading THEN the
        FIRST recorded salt wins -- the salt must stay pinned for the whole sweep, not
        drift to whatever the most recent run happened to pass."""
        append_run_manifest(tmp_path, selection_rule="run one", salt="salt-one")
        append_run_manifest(tmp_path, selection_rule="run two", salt="salt-two")

        result = _load_or_create_salt(tmp_path, explicit_salt=None)

        assert result == "salt-one"
