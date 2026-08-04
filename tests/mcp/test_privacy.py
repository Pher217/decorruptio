"""Tests for `uncorrupt.mcp.privacy.entity_summary` -- the one sanctioned
`Entity -> dict` boundary for the MCP layer.

The central guarantee under test: DOB, nationality, and address can never
be returned by an MCP tool even if a future ingest bug wrote them into
`Entity.properties` -- because `entity_summary` never reads that field at
all, for any entity type.
"""

from __future__ import annotations

import pytest

from uncorrupt.graph.models import Entity
from uncorrupt.mcp.privacy import entity_summary


@pytest.mark.django_db
class TestEntitySummaryShape:
    def test_company_entity_carries_company_number(self):
        """GIVEN a company Entity WHEN summarised THEN company_number is present."""
        entity = Entity.objects.create(
            entity_type="company",
            name="PPE Medpro Ltd",
            registry_scheme="GB-COH",
            registry_id="12410514",
            company_number="12410514",
        )
        summary = entity_summary(entity)
        assert summary["company_number"] == "12410514"

    def test_company_entity_has_no_role_description_key(self):
        """GIVEN a company Entity WHEN summarised THEN no role_description key exists."""
        entity = Entity.objects.create(entity_type="company", name="Acme Ltd")
        summary = entity_summary(entity)
        assert "role_description" not in summary

    def test_person_entity_carries_role_description(self):
        """GIVEN a person Entity WHEN summarised THEN role_description is present."""
        entity = Entity.objects.create(
            entity_type="person",
            name="Jane Smith",
            role_description="MP for Example South",
        )
        summary = entity_summary(entity)
        assert summary["role_description"] == "MP for Example South"

    def test_person_entity_has_no_company_number_key(self):
        """GIVEN a person Entity WHEN summarised THEN no company_number key exists."""
        entity = Entity.objects.create(entity_type="person", name="Jane Smith")
        summary = entity_summary(entity)
        assert "company_number" not in summary

    def test_summary_has_expected_identity_fields(self):
        """GIVEN any Entity WHEN summarised THEN id/type/name/registry fields are present."""
        entity = Entity.objects.create(
            entity_type="company",
            name="Acme Ltd",
            registry_scheme="GB-COH",
            registry_id="00000001",
        )
        summary = entity_summary(entity)
        assert summary["entity_id"] == entity.id
        assert summary["entity_type"] == "company"
        assert summary["name"] == "Acme Ltd"
        assert summary["registry_scheme"] == "GB-COH"
        assert summary["registry_id"] == "00000001"


@pytest.mark.django_db
class TestEntitySummaryNeverLeaksPersonalFields:
    """`entity_summary` must never surface DOB, nationality, or address --
    even when they are present in `Entity.properties` (the ingest-time
    allowlists that normally keep this field clean are convention, not a DB
    constraint; this is the defensive backstop for when that convention
    fails)."""

    _PERSONAL_PROPERTIES = {
        "date_of_birth": "1975-05-05",
        "nationality": "British",
        "residential_address": "1 Test Street, London, SW1A 1AA",
        "country_of_residence": "United Kingdom",
        "occupation": "Company Director",
    }

    def test_properties_key_absent_from_person_summary(self):
        """GIVEN a person Entity with personal fields in properties WHEN summarised
        THEN the raw `properties` key is absent from the output entirely."""
        entity = Entity.objects.create(
            entity_type="person",
            name="Jane Smith",
            role_description="MP for Example South",
            properties=self._PERSONAL_PROPERTIES,
        )
        summary = entity_summary(entity)
        assert "properties" not in summary

    def test_no_personal_value_appears_anywhere_in_person_summary(self):
        """GIVEN a person Entity with DOB/nationality/address stuffed into
        properties WHEN summarised THEN none of those values appear anywhere
        in the serialised output, under any key."""
        entity = Entity.objects.create(
            entity_type="person",
            name="Jane Smith",
            role_description="MP for Example South",
            properties=self._PERSONAL_PROPERTIES,
        )
        summary = entity_summary(entity)
        serialized_values = {str(v) for v in summary.values()}
        for personal_value in self._PERSONAL_PROPERTIES.values():
            assert personal_value not in serialized_values

    def test_properties_key_absent_from_company_summary(self):
        """GIVEN a company Entity with arbitrary data in properties WHEN
        summarised THEN the raw `properties` key is absent from the output."""
        entity = Entity.objects.create(
            entity_type="company",
            name="Acme Ltd",
            registry_scheme="GB-COH",
            registry_id="00000002",
            properties={"anything": "at all"},
        )
        summary = entity_summary(entity)
        assert "properties" not in summary
