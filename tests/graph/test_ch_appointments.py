"""Tests for the Companies House officer-appointments second-hop ingest.

Covers the `selection_rule` provenance tag added alongside the officer-
coverage-expansion work in `ch_officers.py`: `Edge.properties["selection_rule"]`
records why an appointment-hop edge was created, mirroring
`ch_officers.ingest_company_officers` -- set only on first creation, never
overwritten by a later re-ingest under a different rule (get_or_create only
applies `defaults` at creation time).

Also covers the register contract: fetch_officer_appointments/
ingest_officer_appointments refuse to run without a
sources/uk_companies_house_officers.yml register entry (the same entry
ch_officers.py uses -- one CH API, one licence, one register row).
"""

import json
from pathlib import Path

import httpx
import pytest

import uncorrupt.graph.ch_appointments as ch_appointments_module
from uncorrupt.core.errors import RegisterError
from uncorrupt.graph.ch_appointments import fetch_officer_appointments, ingest_officer_appointments
from uncorrupt.graph.ch_officers import API_KEY_ENV_VAR
from uncorrupt.graph.models import Edge, Entity
from uncorrupt.staging.models import Company

APPOINTMENT_ITEM = {
    "appointed_to": {
        "company_number": "00000001",
        "company_name": "Acme Ltd",
        "company_status": "active",
    },
    "officer_role": "director",
    "appointed_on": "2020-01-01",
    "resigned_on": None,
    "links": {"self": "/officers/officer-1/appointments/appt-1"},
}


def _write_cache(tmp_path: Path, officer_id: str, items: list[dict]) -> None:
    (tmp_path / f"{officer_id}.json").write_text(json.dumps(items))


def _make_officer(registry_id: str = "officer-1") -> Entity:
    return Entity.objects.create(
        entity_type="person",
        registry_scheme="GB-COH-OFFICER",
        registry_id=registry_id,
        name="SMITH, John",
    )


@pytest.mark.django_db
class TestIngestOfficerAppointmentsSelectionRule:
    def test_selection_rule_is_stamped_on_newly_created_edge(self, tmp_path):
        """GIVEN a selection_rule WHEN a new appointment-hop edge is created THEN the
        edge's properties record that selection_rule."""
        Company.objects.create(company_number="00000001", company_name="Acme Ltd")
        _make_officer()
        _write_cache(tmp_path, "officer-1", [APPOINTMENT_ITEM])

        ingest_officer_appointments(
            ["officer-1"], tmp_path, selection_rule="universe=procurement-suppliers"
        )

        edge = Edge.objects.get(edge_type="officer_of", target_entity__company_number="00000001")
        assert edge.properties["selection_rule"] == "universe=procurement-suppliers"

    def test_selection_rule_is_not_overwritten_on_re_ingest_under_a_different_rule(self, tmp_path):
        """GIVEN an edge already created under one selection_rule WHEN re-ingested
        (get_or_create matches the existing edge) under a DIFFERENT selection_rule THEN
        the original rule is preserved, not overwritten."""
        Company.objects.create(company_number="00000001", company_name="Acme Ltd")
        _make_officer()
        _write_cache(tmp_path, "officer-1", [APPOINTMENT_ITEM])
        ingest_officer_appointments(["officer-1"], tmp_path, selection_rule="first-rule")

        ingest_officer_appointments(["officer-1"], tmp_path, selection_rule="second-rule")

        edge = Edge.objects.get(edge_type="officer_of", target_entity__company_number="00000001")
        assert edge.properties["selection_rule"] == "first-rule"

    def test_no_selection_rule_leaves_properties_without_the_key(self, tmp_path):
        """GIVEN no selection_rule argument (existing call sites, unchanged) WHEN
        ingesting THEN the edge's properties contain no selection_rule key at all."""
        Company.objects.create(company_number="00000001", company_name="Acme Ltd")
        _make_officer()
        _write_cache(tmp_path, "officer-1", [APPOINTMENT_ITEM])

        ingest_officer_appointments(["officer-1"], tmp_path)

        edge = Edge.objects.get(edge_type="officer_of", target_entity__company_number="00000001")
        assert "selection_rule" not in edge.properties


class TestFetchOfficerAppointmentsCircuitBreaker:
    def test_aborts_sweep_after_max_consecutive_failures(self, tmp_path, monkeypatch):
        """GIVEN every officer fails WHEN max_consecutive_failures=3 over a batch of 10
        THEN the sweep aborts after exactly 3 consecutive failures -- the remaining 7
        officers are never attempted."""
        monkeypatch.setenv(API_KEY_ENV_VAR, "test-key")
        call_count = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(403)

        client = httpx.Client(transport=httpx.MockTransport(handler))
        monkeypatch.setattr(ch_appointments_module.httpx, "Client", lambda *a, **k: client)
        officer_ids = [f"officer-{i}" for i in range(10)]

        counts = fetch_officer_appointments(officer_ids, tmp_path, max_consecutive_failures=3)

        assert counts == {"fetched": 0, "cached": 0, "failed": 3}
        assert call_count == 3


class TestChAppointmentsRegisterContract:
    def test_ingest_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/uk_companies_house_officers.yml cannot be resolved (its
        source_id is absent from the register) WHEN ingest_officer_appointments is
        called THEN it raises RegisterError and writes nothing to the database."""
        monkeypatch.setattr(ch_appointments_module, "SOURCE_ID", "does_not_exist_xyz")
        _write_cache(tmp_path, "officer-1", [APPOINTMENT_ITEM])

        with pytest.raises(RegisterError):
            ingest_officer_appointments(["officer-1"], tmp_path)

    def test_fetch_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/uk_companies_house_officers.yml cannot be resolved WHEN
        fetch_officer_appointments is called THEN it raises RegisterError before
        creating an HTTP client. fetch_officer_appointments (unlike
        fetch_company_officers) takes no injectable client, so httpx.Client itself
        is patched to raise if instantiated -- proving no request could have been
        attempted."""
        monkeypatch.setattr(ch_appointments_module, "SOURCE_ID", "does_not_exist_xyz")

        def _unexpected_client(*args, **kwargs):
            raise AssertionError("fetch_officer_appointments must not create an HTTP client")

        monkeypatch.setattr(ch_appointments_module.httpx, "Client", _unexpected_client)

        with pytest.raises(RegisterError):
            fetch_officer_appointments(["officer-1"], tmp_path)
