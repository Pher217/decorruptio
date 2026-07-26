"""Tests for the graph models (Entity, Alias, Edge, Attestation).

Verifies the core invariants of the relationship-recovery graph:
- Registry ID uniqueness (ADR-004 D2: resolve by ID, never by name)
- Temporal provenance on every edge
- Source citation required on every attestation
- Entity type constraints
- Alias association
"""

import pytest
from django.db import IntegrityError

from uncorrupt.graph.models import Alias, Attestation, Edge, Entity


@pytest.mark.django_db
class TestEntity:
    def test_create_company_entity(self):
        e = Entity.objects.create(
            entity_type="company",
            name="PPE Medpro Ltd",
            registry_scheme="GB-COH",
            registry_id="12410514",
            company_number="12410514",
        )
        assert e.entity_type == "company"
        assert e.registry_id == "12410514"
        assert e.company_number == "12410514"

    def test_create_person_entity(self):
        e = Entity.objects.create(
            entity_type="person",
            name="Jane Smith",
            role_description="MP for Example South",
        )
        assert e.entity_type == "person"
        assert e.role_description == "MP for Example South"

    def test_create_public_body_entity(self):
        e = Entity.objects.create(
            entity_type="public_body",
            name="Department of Health and Social Care",
        )
        assert e.entity_type == "public_body"

    def test_registry_id_unique(self):
        Entity.objects.create(
            entity_type="company",
            name="First Corp",
            registry_scheme="GB-COH",
            registry_id="12345678",
        )
        with pytest.raises(IntegrityError):
            Entity.objects.create(
                entity_type="company",
                name="Second Corp",
                registry_scheme="GB-COH",
                registry_id="12345678",
            )

    def test_multiple_entities_without_registry_id_allowed(self):
        """Entities without registry_id are not constrained by the unique index."""
        e1 = Entity.objects.create(entity_type="person", name="Unknown Person A")
        e2 = Entity.objects.create(entity_type="person", name="Unknown Person B")
        assert e1.pk != e2.pk

    def test_different_schemes_same_id_allowed(self):
        """Same ID under different schemes is a different entity."""
        Entity.objects.create(
            entity_type="company",
            name="UK Corp",
            registry_scheme="GB-COH",
            registry_id="12345678",
        )
        e2 = Entity.objects.create(
            entity_type="company",
            name="Cypriot Corp",
            registry_scheme="CY-RC",
            registry_id="12345678",
        )
        assert e2.registry_scheme == "CY-RC"


@pytest.mark.django_db
class TestAlias:
    def test_create_alias(self):
        entity = Entity.objects.create(
            entity_type="company",
            name="Crisp Websites Ltd",
            registry_scheme="GB-COH",
            registry_id="04458411",
        )
        alias = Alias.objects.create(
            entity=entity,
            name="PestFix",
            alias_type="trading_as",
            source_name="Companies House",
        )
        assert alias.entity == entity
        assert alias.name == "PestFix"
        assert alias.alias_type == "trading_as"

    def test_multiple_aliases_for_one_entity(self):
        entity = Entity.objects.create(entity_type="company", name="Example Ltd")
        Alias.objects.create(
            entity=entity, name="Example UK", alias_type="trading_as", source_name="CH"
        )
        Alias.objects.create(
            entity=entity, name="Old Name Ltd", alias_type="former_name", source_name="CH"
        )
        assert entity.aliases.count() == 2

    def test_alias_cascade_delete(self):
        entity = Entity.objects.create(entity_type="company", name="Doomed Ltd")
        Alias.objects.create(entity=entity, name="Doomed Trading", source_name="CH")
        entity.delete()
        assert Alias.objects.filter(name="Doomed Trading").count() == 0

    def test_alias_requires_source(self):
        """An alias without a source is a vacuous claim — the field must exist and be set."""
        entity = Entity.objects.create(entity_type="company", name="Sourced Ltd")
        alias = Alias.objects.create(
            entity=entity,
            name="Sourced Trading",
            source_name="Companies House",
            source_url="https://find-and-update.company-information.service.gov.uk/",
        )
        assert alias.source_name == "Companies House"
        assert alias.source_url is not None


@pytest.mark.django_db
class TestEdge:
    """Tests for Edge and Attestation — an Edge is a claim, an Attestation is its citation."""

    def _create_pair(self):
        source = Entity.objects.create(
            entity_type="person",
            name="Referrer MP",
            role_description="MP for X",
        )
        target = Entity.objects.create(
            entity_type="company",
            name="PPE Medpro Ltd",
            registry_scheme="GB-COH",
            registry_id="12410514",
            company_number="12410514",
        )
        return source, target

    def test_create_donation_edge(self):
        donor, recipient = self._create_pair()
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=donor,
            target_entity=recipient,
            valid_from="2020-04-01",
            valid_to="2020-04-01",
            amount_cents=5000000,
            currency="GBP",
        )
        Attestation.objects.create(
            edge=edge,
            source_name="Electoral Commission",
            source_url="https://example.com/ec/12345",
            source_reference="EC-12345",
        )
        assert edge.edge_type == "donation"
        attestation = edge.attestations.get()
        assert attestation.source_name == "Electoral Commission"
        assert edge.amount_cents == 5000000
        assert edge.currency == "GBP"

    def test_donation_edge_amount_cents_round_trips_as_integer(self):
        """Money is integer cents, not a float — must survive a DB round-trip exactly."""
        donor, recipient = self._create_pair()
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=donor,
            target_entity=recipient,
            amount_cents=1234567,
            currency="GBP",
        )
        fetched = Edge.objects.get(pk=edge.pk)
        assert fetched.amount_cents == 1234567
        assert isinstance(fetched.amount_cents, int)

    def test_attestation_defaults_to_identifier_match_full_confidence(self):
        source, target = self._create_pair()
        edge = Edge.objects.create(
            edge_type="officer_of",
            source_entity=source,
            target_entity=target,
        )
        attestation = Attestation.objects.create(
            edge=edge,
            source_name="Companies House",
        )
        assert attestation.match_confidence == 1.0
        assert attestation.match_method == "identifier"

    def test_tier2_name_matched_attestation_records_lower_confidence(self):
        """An attestation built from tier-2 name resolution must declare its weaker provenance."""
        source, target = self._create_pair()
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
        )
        attestation = Attestation.objects.create(
            edge=edge,
            source_name="Electoral Commission",
            match_confidence=0.9,
            match_method="exact_name",
        )
        assert attestation.match_confidence < 1.0
        assert attestation.match_method == "exact_name"

    def test_create_officer_edge(self):
        person, company = self._create_pair()
        edge = Edge.objects.create(
            edge_type="officer_of",
            source_entity=person,
            target_entity=company,
            valid_from="2020-05-12",
            properties={"officer_role": "director"},
        )
        Attestation.objects.create(
            edge=edge,
            source_name="Companies House",
            source_reference="CH-12410514-officer-1",
        )
        assert edge.edge_type == "officer_of"
        assert edge.valid_to is None

    def test_create_referred_to_lane_edge(self):
        referrer, supplier = self._create_pair()
        edge = Edge.objects.create(
            edge_type="referred_to_lane",
            source_entity=referrer,
            target_entity=supplier,
            valid_from="2020-04-01",
            valid_to="2020-10-01",
        )
        Attestation.objects.create(
            edge=edge,
            source_name="DHSC High Priority Lane",
            source_reference="DHSC-HPL-row-42",
        )
        assert edge.edge_type == "referred_to_lane"
        assert edge.source_entity == referrer
        assert edge.target_entity == supplier

    def test_attestation_source_name_required(self):
        """Every attestation must carry a source citation — this is the core of Phase 1."""
        referrer, supplier = self._create_pair()
        edge = Edge.objects.create(
            edge_type="referred_to_lane",
            source_entity=referrer,
            target_entity=supplier,
        )
        with pytest.raises(IntegrityError):
            Attestation.objects.create(
                edge=edge,
                source_name="",  # empty string, not null — must be rejected
            )

    def test_attestation_match_confidence_out_of_range_rejected(self):
        """match_confidence must be within [0, 1] — a probability, not an arbitrary float."""
        referrer, supplier = self._create_pair()
        edge = Edge.objects.create(
            edge_type="referred_to_lane",
            source_entity=referrer,
            target_entity=supplier,
        )
        with pytest.raises(IntegrityError):
            Attestation.objects.create(
                edge=edge,
                source_name="Test",
                match_confidence=1.5,
            )

    def test_edge_valid_to_before_valid_from_rejected(self):
        """An interval that ends before it starts is inverted data, not a real claim."""
        referrer, supplier = self._create_pair()
        with pytest.raises(IntegrityError):
            Edge.objects.create(
                edge_type="referred_to_lane",
                source_entity=referrer,
                target_entity=supplier,
                valid_from="2020-04-01",
                valid_to="2020-01-01",
            )

    def test_edge_cascade_delete(self):
        source, target = self._create_pair()
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
            valid_from="2020-01-01",
        )
        Attestation.objects.create(edge=edge, source_name="Electoral Commission")
        attestation_pk = edge.attestations.first().pk
        source.delete()
        assert Edge.objects.filter(pk=edge.pk).count() == 0
        assert Attestation.objects.filter(pk=attestation_pk).count() == 0

    def test_outgoing_and_incoming_relations(self):
        source, target = self._create_pair()
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
            valid_from="2020-01-01",
        )
        Attestation.objects.create(edge=edge, source_name="EC")
        assert source.outgoing_edges.count() == 1
        assert target.incoming_edges.count() == 1
        assert source.incoming_edges.count() == 0
        assert target.outgoing_edges.count() == 0

    def test_temporal_provenance_fields_exist(self):
        """Every edge must carry valid_from / valid_to for temporal correctness."""
        source, target = self._create_pair()
        edge = Edge.objects.create(
            edge_type="officer_of",
            source_entity=source,
            target_entity=target,
            valid_from="2019-06-01",
            valid_to="2021-01-15",
        )
        Attestation.objects.create(edge=edge, source_name="Companies House")
        assert edge.valid_from is not None
        assert edge.valid_to is not None
        assert edge.valid_from < edge.valid_to

    def test_self_reference_allowed(self):
        """An entity can have a self-referential edge (e.g. company holding company)."""
        entity = Entity.objects.create(
            entity_type="company",
            name="Self Holding Ltd",
            registry_scheme="GB-COH",
            registry_id="11111111",
        )
        edge = Edge.objects.create(
            edge_type="associate_of",
            source_entity=entity,
            target_entity=entity,
        )
        Attestation.objects.create(edge=edge, source_name="Test")
        assert edge.source_entity == edge.target_entity
