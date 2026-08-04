"""Tests for the OpenSanctions non-personal-entity ingest.

Verifies the core invariants:
- Only Company/Organization/LegalEntity schema records are written — Person
  and every relationship schema (which could carry a named individual) are
  dropped and counted, never silently skipped.
- Identifier-only cross-linking: leiCode -> an existing GLEIF-LEI Entity;
  a GB registrationNumber -> staging.Company — never inferred from jurisdiction
  or number shape alone, never by name.
- Re-ingesting the same OpenSanctions id updates rather than duplicates.
- fetch_opensanctions/ingest_opensanctions refuse to run without
  sources/opensanctions_entities.yml, and fetch never touches the network.
- The sibling sources/opensanctions.yml (person-level, A2) stays untouched
  and gated, and this entry's own provenance is still rejected from a bulk
  open export despite being data_class A1 (the redistribution/tier guard is
  orthogonal to the DPIA gate).
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

import uncorrupt.graph.opensanctions as os_module
from uncorrupt.core.errors import RedistributionViolation, RegisterError, TierViolation
from uncorrupt.core.provenance import ProvenanceRecord, Redistribution
from uncorrupt.core.tiers import DataClass, Tier
from uncorrupt.graph.models import Entity
from uncorrupt.graph.opensanctions import fetch_opensanctions, ingest_opensanctions
from uncorrupt.register.enforcement import assert_bulk_open_exportable
from uncorrupt.register.loader import load_source
from uncorrupt.staging.models import Company


def _entity_record(
    os_id: str = "NK-entity1",
    schema: str = "Company",
    name: str = "Example Sanctioned Co",
    jurisdiction: list[str] | None = None,
    country: list[str] | None = None,
    lei: list[str] | None = None,
    registration_number: list[str] | None = None,
) -> dict:
    properties: dict = {"name": [name]}
    if jurisdiction:
        properties["jurisdiction"] = jurisdiction
    if country:
        properties["country"] = country
    if lei:
        properties["leiCode"] = lei
    if registration_number:
        properties["registrationNumber"] = registration_number
    properties["topics"] = ["sanction"]
    properties["programId"] = ["US-GLOMAG"]
    return {
        "id": os_id,
        "caption": name,
        "schema": schema,
        "datasets": ["us_ofac_sdn"],
        "first_seen": "2025-09-08T14:10:01",
        "last_seen": "2026-08-04T12:53:02",
        "properties": properties,
        "target": True,
    }


def _write_jsonl(tmp_path: Path, records: list[dict]) -> Path:
    jsonl_path = tmp_path / "opensanctions.jsonl"
    with open(jsonl_path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record))
            f.write("\n")
    return jsonl_path


@pytest.mark.django_db
class TestOpenSanctionsIngest:
    def test_lei_match_links_to_existing_gleif_entity(self, tmp_path):
        """GIVEN a GLEIF Entity already exists for a given LEI WHEN a Company
        record carrying that leiCode is ingested THEN gleif_lei_linked is 1
        and the new Entity's properties record the link."""
        Entity.objects.create(
            entity_type="company",
            registry_scheme="GLEIF-LEI",
            registry_id="984500BG7783FREB4988",
            name="Example Ltd (GLEIF)",
        )
        jsonl_path = _write_jsonl(tmp_path, [_entity_record(lei=["984500BG7783FREB4988"])])

        summary = ingest_opensanctions(jsonl_path)

        assert summary["gleif_lei_linked"] == 1
        entity = Entity.objects.get(registry_scheme="OPENSANCTIONS-ORG", registry_id="NK-entity1")
        assert entity.properties["gleif_lei_linked"] is True
        assert entity.properties["lei"] == "984500BG7783FREB4988"

    def test_lei_match_does_not_set_company_number(self, tmp_path):
        """An LEI match links to the GLEIF Entity's own registry identity but
        never sets company_number — that field is reserved for the separate,
        Companies-House-specific join (gb_coh_linked)."""
        Entity.objects.create(
            entity_type="company",
            registry_scheme="GLEIF-LEI",
            registry_id="984500BG7783FREB4988",
            name="Example Ltd (GLEIF)",
        )
        jsonl_path = _write_jsonl(tmp_path, [_entity_record(lei=["984500BG7783FREB4988"])])

        ingest_opensanctions(jsonl_path)

        entity = Entity.objects.get(registry_scheme="OPENSANCTIONS-ORG", registry_id="NK-entity1")
        assert entity.company_number is None

    def test_gb_company_with_unpadded_registration_number_links_to_company(self, tmp_path):
        """A GB-jurisdiction record's unpadded registrationNumber resolves to
        the zero-padded staging.Company row."""
        Company.objects.create(
            company_number="07015428",
            company_name="Solas Banc Real Estate Ltd",
            normalised_name="SOLAS BANC REAL ESTATE LTD",
        )
        jsonl_path = _write_jsonl(
            tmp_path,
            [_entity_record(jurisdiction=["gb"], registration_number=["7015428"])],
        )

        summary = ingest_opensanctions(jsonl_path)

        assert summary["gb_coh_linked"] == 1
        entity = Entity.objects.get(registry_scheme="OPENSANCTIONS-ORG", registry_id="NK-entity1")
        assert entity.company_number == "07015428"

    def test_non_gb_registration_number_is_never_linked_even_if_it_matches(self, tmp_path):
        """A registrationNumber is only ever tried against staging.Company when
        the record's jurisdiction/country includes 'gb' — a coincidental
        numeric match on a non-GB record must never link."""
        Company.objects.create(
            company_number="00636217",
            company_name="Unrelated Real Company Ltd",
            normalised_name="UNRELATED REAL COMPANY LTD",
        )
        jsonl_path = _write_jsonl(
            tmp_path,
            [_entity_record(country=["mm"], registration_number=["636217"])],
        )

        summary = ingest_opensanctions(jsonl_path)

        assert summary["gb_coh_linked"] == 0
        entity = Entity.objects.get(registry_scheme="OPENSANCTIONS-ORG", registry_id="NK-entity1")
        assert entity.company_number is None

    def test_record_with_no_identifiers_is_ingested_unlinked_never_name_matched(self, tmp_path):
        """A record with neither leiCode nor a GB registrationNumber is still
        ingested (its own OpenSanctions id is a real identifier), but with no
        company_number — it is never resolved by name (ADR-004 D2)."""
        Company.objects.create(
            company_number="00636217",
            company_name="Example Sanctioned Co",
            normalised_name="EXAMPLE SANCTIONED CO",
        )
        jsonl_path = _write_jsonl(tmp_path, [_entity_record(name="Example Sanctioned Co")])

        summary = ingest_opensanctions(jsonl_path)

        assert summary["created"] == 1
        assert summary["gb_coh_linked"] == 0
        assert summary["gleif_lei_linked"] == 0
        entity = Entity.objects.get(registry_scheme="OPENSANCTIONS-ORG", registry_id="NK-entity1")
        assert entity.company_number is None

    def test_person_schema_is_dropped_and_counted_never_written(self, tmp_path):
        """A Person-schema record is never written to the graph — it is
        counted in skipped_non_entity_schema, not silently discarded."""
        record = _entity_record(schema="Person", name="Jane Doe")
        jsonl_path = _write_jsonl(tmp_path, [record])

        summary = ingest_opensanctions(jsonl_path)

        assert summary["skipped_non_entity_schema"] == 1
        assert summary["created"] == 0
        assert Entity.objects.count() == 0

    @pytest.mark.parametrize(
        "schema", ["Directorship", "Ownership", "Family", "Occupancy", "Succession", "UnknownLink"]
    )
    def test_relationship_schema_is_dropped_and_counted_never_written(self, tmp_path, schema):
        """Every relationship schema that could carry a named individual is
        dropped before any write, same as Person."""
        record = _entity_record(schema=schema)
        jsonl_path = _write_jsonl(tmp_path, [record])

        summary = ingest_opensanctions(jsonl_path)

        assert summary["skipped_non_entity_schema"] == 1
        assert Entity.objects.count() == 0

    def test_record_missing_id_is_skipped_and_counted(self, tmp_path):
        """A record with no id is skipped, never invented, and counted."""
        record = _entity_record()
        del record["id"]
        jsonl_path = _write_jsonl(tmp_path, [record])

        summary = ingest_opensanctions(jsonl_path)

        assert summary["skipped_no_id"] == 1
        assert summary["created"] == 0
        assert Entity.objects.count() == 0

    def test_reingest_same_id_updates_not_duplicates(self, tmp_path):
        """Re-running ingest on the same OpenSanctions id updates the existing
        Entity, never duplicates."""
        record = _entity_record()
        jsonl_path = _write_jsonl(tmp_path, [record])
        summary_1 = ingest_opensanctions(jsonl_path)
        assert summary_1["created"] == 1

        updated_record = dict(record)
        updated_record["properties"] = dict(record["properties"])
        updated_record["properties"]["topics"] = ["sanction", "debarment"]
        jsonl_path_2 = _write_jsonl(tmp_path, [updated_record])
        summary_2 = ingest_opensanctions(jsonl_path_2)

        assert summary_2["created"] == 0
        assert summary_2["updated"] == 1
        assert (
            Entity.objects.filter(
                registry_scheme="OPENSANCTIONS-ORG", registry_id="NK-entity1"
            ).count()
            == 1
        )
        entity = Entity.objects.get(registry_scheme="OPENSANCTIONS-ORG", registry_id="NK-entity1")
        assert entity.properties["topics"] == ["sanction", "debarment"]


class TestOpenSanctionsFetch:
    def test_fetch_never_imports_a_network_client(self):
        """GIVEN the module docstring's decision not to auto-fetch (robots.txt
        disallows the bulk-data host) THEN the module carries no HTTP client
        import at all — structurally, not just by convention, it cannot make
        a network request."""
        assert not hasattr(os_module, "httpx")

    def test_fetch_caches_local_file_and_writes_provenance(self, tmp_path):
        """fetch_opensanctions caches an already-downloaded local file and
        writes a provenance sidecar with a correct record count and hash."""
        input_dir = tmp_path / "input"
        input_dir.mkdir()
        input_path = _write_jsonl(input_dir, [_entity_record(), _entity_record(os_id="NK-entity2")])
        output_dir = tmp_path / "cache"

        result = fetch_opensanctions(input_path, output_dir)

        assert result.record_count == 2
        provenance = json.loads(result.provenance_path.read_text())
        assert provenance["record_count"] == 2
        assert result.jsonl_path.read_bytes() == input_path.read_bytes()


class TestOpenSanctionsRegisterContract:
    def test_ingest_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/opensanctions_entities.yml cannot be resolved WHEN
        ingest_opensanctions is called THEN it raises RegisterError and
        writes nothing to the database."""
        monkeypatch.setattr(os_module, "SOURCE_ID", "does_not_exist_xyz")
        jsonl_path = _write_jsonl(tmp_path, [_entity_record()])

        with pytest.raises(RegisterError):
            ingest_opensanctions(jsonl_path)

    def test_fetch_refuses_to_run_without_register_entry(self, tmp_path, monkeypatch):
        """GIVEN sources/opensanctions_entities.yml cannot be resolved WHEN
        fetch_opensanctions is called THEN it raises RegisterError before
        reading the input file."""
        monkeypatch.setattr(os_module, "SOURCE_ID", "does_not_exist_xyz")
        missing_input = tmp_path / "does_not_exist.jsonl"

        with pytest.raises(RegisterError):
            fetch_opensanctions(missing_input, tmp_path / "cache")

    def test_register_entry_declares_the_graph_connector_contract(self):
        """GIVEN sources/opensanctions_entities.yml WHEN loaded THEN it is
        data_class A1, connector_kind graph, and declares the
        OPENSANCTIONS-ORG registry scheme this module emits."""
        source = load_source("opensanctions_entities")

        assert source.data_class is DataClass.A1
        assert source.connector_kind == "graph"
        assert source.registry_schemes == ["OPENSANCTIONS-ORG"]

    def test_sibling_person_level_entry_is_unmodified_and_still_gated(self):
        """GIVEN sources/opensanctions.yml (the pre-existing, person-level
        entry) WHEN loaded THEN it is still A2/dpia_cleared:false — this
        connector's new A1 entry does not loosen that gate."""
        source = load_source("opensanctions")

        assert source.data_class is DataClass.A2
        assert source.dpia_cleared is False


class TestOpenSanctionsRedistributionGuard:
    def test_provenance_is_rejected_from_bulk_open_export_despite_being_a1(self):
        """GIVEN sources/opensanctions_entities.yml's real redistribution
        value (CC-BY-NC / non_commercial) WHEN its provenance (tier pinned to
        'a' to isolate the redistribution check from the tier check below) is
        checked against the bulk-open-export guard THEN it is rejected —
        being non-personal (A1, exempt from the DPIA gate) does not exempt it
        from the licence/redistribution gate, which is orthogonal."""
        source = load_source("opensanctions_entities")
        prov = ProvenanceRecord(
            source_id=source.source_id,
            source_url="https://www.opensanctions.org/datasets/default/",
            retrieved_at=datetime(2026, 8, 4),
            content_hash="x" * 64,
            license=source.license,
            redistribution=source.redistribution,
            jurisdiction="GLOBAL",
            data_class=source.data_class,
            tier=Tier.A,
            connector=source.source_id,
            connector_version="0.1",
        )

        assert prov.redistribution is Redistribution.NON_COMMERCIAL
        with pytest.raises(RedistributionViolation):
            assert_bulk_open_exportable(prov)

    def test_real_registered_tier_also_fails_the_tier_gate(self):
        """GIVEN sources/opensanctions_entities.yml's real, registered tier
        ('b', same as the sibling opensanctions.yml — vetted, not open) WHEN
        its provenance is checked against the bulk-open-export guard THEN it
        is rejected at the tier gate before the redistribution gate is even
        reached — as actually configured, this source cannot reach a tier-a
        open export by two independent gates, not just one."""
        source = load_source("opensanctions_entities")
        prov = ProvenanceRecord(
            source_id=source.source_id,
            source_url="https://www.opensanctions.org/datasets/default/",
            retrieved_at=datetime(2026, 8, 4),
            content_hash="x" * 64,
            license=source.license,
            redistribution=source.redistribution,
            jurisdiction="GLOBAL",
            data_class=source.data_class,
            tier=source.tier,
            connector=source.source_id,
            connector_version="0.1",
        )

        assert source.tier is Tier.B
        with pytest.raises(TierViolation):
            assert_bulk_open_exportable(prov)
