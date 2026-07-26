"""Tests for the FtM export management command."""

import json

import pytest
from django.core.management import call_command

from uncorrupt.graph.models import Attestation, Edge, Entity


@pytest.mark.django_db
class TestFtMExport:
    def test_export_empty_graph(self, tmp_path):
        """Exporting an empty graph produces an empty file."""
        output = tmp_path / "export.jsonl"
        call_command("export_ftm", output=str(output))
        assert output.exists()
        lines = output.read_text().strip().split("\n")
        assert lines == [""]

    def test_export_entity_company(self, tmp_path):
        """A company entity exports as FtM Company with registrationNumber."""
        Entity.objects.create(
            entity_type="company",
            name="PPE Medpro Ltd",
            registry_scheme="GB-COH",
            registry_id="12410514",
            company_number="12410514",
        )
        output = tmp_path / "export.jsonl"
        call_command("export_ftm", output=str(output))

        lines = [json.loads(line) for line in output.read_text().strip().split("\n") if line]
        assert len(lines) == 1
        entity = lines[0]
        assert entity["schema"] == "Company"
        assert entity["properties"]["name"] == ["PPE Medpro Ltd"]
        assert entity["properties"]["registrationNumber"] == ["12410514"]
        assert entity["properties"]["registerId"] == ["GB-COH:12410514"]

    def test_export_entity_person(self, tmp_path):
        """A person entity exports as FtM Person with title."""
        Entity.objects.create(
            entity_type="person",
            name="Lord Adonis",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="3743",
            role_description="Member of the House of Lords",
        )
        output = tmp_path / "export.jsonl"
        call_command("export_ftm", output=str(output))

        lines = [json.loads(line) for line in output.read_text().strip().split("\n") if line]
        assert len(lines) == 1
        entity = lines[0]
        assert entity["schema"] == "Person"
        assert entity["properties"]["title"] == ["Member of the House of Lords"]

    def test_export_edge_with_attestation(self, tmp_path):
        """An edge with attestation exports as FtM entity with provenance."""
        from datetime import UTC, datetime

        source = Entity.objects.create(
            entity_type="company", name="Donor Ltd", company_number="12345678"
        )
        target = Entity.objects.create(entity_type="political_party", name="Labour Party")
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
            amount_cents=100000,
            currency="GBP",
        )
        Attestation.objects.create(
            edge=edge,
            source_name="Electoral Commission",
            source_reference="ECRef123",
            match_confidence=1.0,
            observed_at=datetime(2026, 7, 23, 12, 0, tzinfo=UTC),
        )
        output = tmp_path / "export.jsonl"
        call_command("export_ftm", output=str(output))

        lines = [json.loads(line) for line in output.read_text().strip().split("\n") if line]
        # 2 entities + 1 edge = 3 lines
        assert len(lines) == 3

        # Find the edge
        edge_line = [line for line in lines if line["id"].startswith("uncorrupt:edge:")][0]
        assert edge_line["schema"] == "Payment"
        assert edge_line["properties"]["starter"] == [f"uncorrupt:entity:{source.pk}"]
        assert edge_line["properties"]["ender"] == [f"uncorrupt:entity:{target.pk}"]
        assert "1000.00 GBP" in edge_line["properties"]["amount"][0]
        assert len(edge_line["properties"]["provenance"]) == 1

    def test_export_edge_dates(self, tmp_path):
        """Edge valid_from/valid_to export as startDate/endDate."""
        from datetime import date

        source = Entity.objects.create(entity_type="person", name="Officer")
        target = Entity.objects.create(entity_type="company", name="Company Ltd")
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=source,
            target_entity=target,
            valid_from=date(2020, 1, 15),
            valid_to=date(2023, 6, 30),
        )
        output = tmp_path / "export.jsonl"
        call_command("export_ftm", output=str(output))

        lines = [json.loads(line) for line in output.read_text().strip().split("\n") if line]
        edge_line = [line for line in lines if line["id"].startswith("uncorrupt:edge:")][0]
        assert edge_line["properties"]["startDate"] == ["2020-01-15"]
        assert edge_line["properties"]["endDate"] == ["2023-06-30"]
        assert edge_line["schema"] == "Directorship"
