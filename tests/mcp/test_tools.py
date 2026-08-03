"""Tests for `uncorrupt.mcp.tools` -- the read-only exploration tool functions.

GIVEN/WHEN/THEN, one behaviour per test, exact assertions.
"""

from __future__ import annotations

import pytest
from scripts import phase_c_paths

from uncorrupt.graph import ch_officers
from uncorrupt.graph.models import Alias, Attestation, Edge, Entity
from uncorrupt.mcp import tools


class TestFindPathsDelegatesToPhaseCPaths:
    """The anti-script-sprawl guarantee: no second traversal implementation."""

    def test_find_paths_is_the_same_function_object_as_phase_c_paths(self):
        """GIVEN the module under test WHEN imported THEN its path-search
        function IS `scripts.phase_c_paths.find_paths` -- not a copy, not a
        reimplementation with matching behaviour, the identical object."""
        assert tools._phase_c_find_paths is phase_c_paths.find_paths

    def test_build_adjacency_is_the_same_function_object_as_phase_c_paths(self):
        """GIVEN the module under test WHEN imported THEN its adjacency
        builder IS `scripts.phase_c_paths.build_adjacency`."""
        assert tools.build_adjacency is phase_c_paths.build_adjacency


@pytest.mark.django_db
class TestResolveEntity:
    def test_no_filters_raises_value_error(self):
        """GIVEN no search parameters WHEN resolve_entity is called THEN it raises ValueError."""
        with pytest.raises(ValueError):
            tools.resolve_entity()

    def test_registry_scheme_alone_raises_value_error(self):
        """GIVEN only registry_scheme (no name/company_number/registry_id)
        WHEN resolve_entity is called THEN it raises ValueError -- a scheme
        alone is a filter, not a lookup anchor."""
        with pytest.raises(ValueError):
            tools.resolve_entity(registry_scheme="GB-COH")

    def test_ambiguous_name_returns_every_candidate_never_one_guess(self):
        """GIVEN two distinct companies matching the same name search WHEN
        resolve_entity(name=...) is called THEN both are returned as
        candidates -- it never silently picks one."""
        Entity.objects.create(
            entity_type="company",
            name="CLOSE BROTHERS LIMITED",
            registry_scheme="GB-COH",
            registry_id="00195626",
            company_number="00195626",
        )
        Entity.objects.create(
            entity_type="company",
            name="CLOSE BROTHERS HOLDINGS LIMITED",
            registry_scheme="GB-COH",
            registry_id="06582618",
            company_number="06582618",
        )
        result = tools.resolve_entity(name="CLOSE BROTHERS")
        assert len(result["candidates"]) == 2

    def test_registry_id_and_scheme_resolves_the_unique_entity(self):
        """GIVEN a registry_scheme+registry_id pair WHEN resolve_entity is
        called THEN exactly the one entity with that identifier is returned."""
        Entity.objects.create(
            entity_type="company",
            name="PPE Medpro Ltd",
            registry_scheme="GB-COH",
            registry_id="12410514",
            company_number="12410514",
        )
        Entity.objects.create(
            entity_type="company",
            name="Some Other Ltd",
            registry_scheme="GB-COH",
            registry_id="99999999",
            company_number="99999999",
        )
        result = tools.resolve_entity(registry_scheme="GB-COH", registry_id="12410514")
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["registry_id"] == "12410514"

    def test_company_number_resolves_the_matching_entity(self):
        """GIVEN a company_number WHEN resolve_entity is called THEN the
        entity carrying that company_number is returned."""
        Entity.objects.create(
            entity_type="company",
            name="PestFix Group Ltd",
            registry_scheme="GB-COH",
            registry_id="04458411",
            company_number="04458411",
        )
        result = tools.resolve_entity(company_number="04458411")
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["company_number"] == "04458411"

    def test_no_match_returns_empty_candidates_list(self):
        """GIVEN a name that matches nothing WHEN resolve_entity is called
        THEN candidates is an empty list, not an error."""
        result = tools.resolve_entity(name="Nonexistent Company XYZ")
        assert result["candidates"] == []

    def test_name_matches_via_alias(self):
        """GIVEN an entity known only by a trading-name Alias WHEN
        resolve_entity(name=<alias>) is called THEN the aliased entity is
        returned as a candidate."""
        entity = Entity.objects.create(
            entity_type="company",
            name="Crisp Websites Ltd",
            registry_scheme="GB-COH",
            registry_id="04458411",
        )
        Alias.objects.create(
            entity=entity,
            name="PestFix",
            alias_type="trading_as",
            source_name="Companies House",
        )
        result = tools.resolve_entity(name="PestFix")
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["entity_id"] == entity.id

    def test_entity_type_narrows_results(self):
        """GIVEN a name shared by a company and a person WHEN
        resolve_entity(name=..., entity_type="company") is called THEN only
        the company is returned."""
        Entity.objects.create(entity_type="company", name="Example")
        Entity.objects.create(entity_type="person", name="Example")
        result = tools.resolve_entity(name="Example", entity_type="company")
        assert len(result["candidates"]) == 1
        assert result["candidates"][0]["entity_type"] == "company"

    def test_limit_caps_the_number_of_candidates(self):
        """GIVEN more matches than `limit` WHEN resolve_entity is called
        THEN no more than `limit` candidates are returned."""
        for i in range(5):
            Entity.objects.create(entity_type="company", name=f"Widget Co {i}")
        result = tools.resolve_entity(name="Widget Co", limit=2)
        assert len(result["candidates"]) == 2

    def test_limit_is_echoed_as_the_effective_value_used(self):
        """GIVEN an ordinary within-range limit WHEN resolve_entity is
        called THEN the response's limit key equals what was requested."""
        Entity.objects.create(entity_type="company", name="Widget Co")
        result = tools.resolve_entity(name="Widget Co", limit=5)
        assert result["limit"] == 5

    def test_oversized_limit_is_clamped_to_the_server_side_ceiling(self):
        """GIVEN a caller-supplied limit far beyond the server-side ceiling
        WHEN resolve_entity is called THEN the response's limit key reports
        the CLAMPED effective value, not the raw requested value -- the
        clamp is silent-proof, not silent."""
        Entity.objects.create(entity_type="company", name="Widget Co")
        result = tools.resolve_entity(name="Widget Co", limit=100_000)
        assert result["limit"] == tools._MAX_RESOLVE_LIMIT

    def test_oversized_limit_never_returns_more_than_the_ceiling_of_candidates(self):
        """GIVEN more matching entities than the server-side ceiling WHEN
        resolve_entity is called with an oversized limit THEN no more than
        the ceiling's worth of candidates come back."""
        for i in range(tools._MAX_RESOLVE_LIMIT + 10):
            Entity.objects.create(entity_type="company", name=f"Bulk Co {i}")
        result = tools.resolve_entity(name="Bulk Co", limit=100_000)
        assert len(result["candidates"]) == tools._MAX_RESOLVE_LIMIT

    def test_zero_or_negative_limit_is_clamped_up_to_one(self):
        """GIVEN a caller-supplied limit of zero WHEN resolve_entity is
        called THEN the effective limit is clamped up to 1, not 0."""
        Entity.objects.create(entity_type="company", name="Widget Co")
        result = tools.resolve_entity(name="Widget Co", limit=0)
        assert result["limit"] == 1


@pytest.mark.django_db
class TestGetEntity:
    def test_returns_entity_summary_fields(self):
        """GIVEN an existing entity WHEN get_entity is called THEN the
        summary's identity fields match the stored entity."""
        entity = Entity.objects.create(
            entity_type="company",
            name="Acme Ltd",
            registry_scheme="GB-COH",
            registry_id="00000001",
            company_number="00000001",
        )
        result = tools.get_entity(entity.id)
        assert result["entity_id"] == entity.id
        assert result["name"] == "Acme Ltd"

    def test_edge_counts_combine_outgoing_and_incoming_by_type(self):
        """GIVEN an entity with one outgoing donation edge and one incoming
        officer_of edge WHEN get_entity is called THEN edge_counts_by_type
        reports one of each type."""
        person = Entity.objects.create(entity_type="person", name="Some MP")
        company_a = Entity.objects.create(entity_type="company", name="Company A")
        company_b = Entity.objects.create(entity_type="company", name="Company B")
        Edge.objects.create(edge_type="donation", source_entity=person, target_entity=company_a)
        Edge.objects.create(edge_type="officer_of", source_entity=company_b, target_entity=person)

        result = tools.get_entity(person.id)
        assert result["edge_counts_by_type"] == {"donation": 1, "officer_of": 1}

    def test_entity_with_no_edges_has_empty_counts(self):
        """GIVEN an entity with no edges WHEN get_entity is called THEN
        edge_counts_by_type is an empty dict."""
        entity = Entity.objects.create(entity_type="company", name="Isolated Ltd")
        result = tools.get_entity(entity.id)
        assert result["edge_counts_by_type"] == {}

    def test_nonexistent_id_raises_does_not_exist(self):
        """GIVEN an id with no matching row WHEN get_entity is called THEN
        Entity.DoesNotExist is raised."""
        with pytest.raises(Entity.DoesNotExist):
            tools.get_entity(999999)


@pytest.mark.django_db
class TestFindPaths:
    def test_direct_edge_is_returned_as_a_one_hop_path(self):
        """GIVEN a direct edge between two entities WHEN find_paths is
        called with max_hops=1 THEN one path of one edge is returned."""
        referrer = Entity.objects.create(entity_type="person", name="Referrer MP")
        supplier = Entity.objects.create(entity_type="company", name="Supplier Ltd")
        edge = Edge.objects.create(
            edge_type="referred_to_lane",
            source_entity=referrer,
            target_entity=supplier,
            valid_from="2020-04-01",
        )
        Attestation.objects.create(edge=edge, source_name="DHSC High Priority Lane")

        result = tools.find_paths(referrer.id, supplier.id, max_hops=1)
        assert len(result["paths"]) == 1
        [path] = result["paths"]
        assert path["hops"] == 1
        [serialized_edge] = path["edges"]
        assert serialized_edge["edge_type"] == "referred_to_lane"
        assert serialized_edge["valid_from"] == "2020-04-01"
        assert serialized_edge["attesting_sources"] == ["DHSC High Priority Lane"]

    def test_two_hop_path_via_intermediate_entity_is_found(self):
        """GIVEN referrer -> company_x -> supplier (via a shared
        directorship) with no direct edge WHEN find_paths is called with
        max_hops=2 THEN a two-edge path is returned."""
        referrer = Entity.objects.create(entity_type="person", name="Referrer MP")
        shared_company = Entity.objects.create(entity_type="company", name="Shared Ltd")
        supplier = Entity.objects.create(entity_type="company", name="Supplier Ltd")
        e1 = Edge.objects.create(
            edge_type="officer_of", source_entity=referrer, target_entity=shared_company
        )
        Attestation.objects.create(edge=e1, source_name="Companies House")
        e2 = Edge.objects.create(
            edge_type="officer_of", source_entity=supplier, target_entity=shared_company
        )
        Attestation.objects.create(edge=e2, source_name="Companies House")

        result = tools.find_paths(referrer.id, supplier.id, max_hops=2)
        assert len(result["paths"]) == 1
        assert result["paths"][0]["hops"] == 2

    def test_no_path_within_max_hops_returns_empty_paths(self):
        """GIVEN two entities with no connecting edge WHEN find_paths is
        called THEN paths is an empty list."""
        a = Entity.objects.create(entity_type="person", name="Isolated A")
        b = Entity.objects.create(entity_type="company", name="Isolated B")
        result = tools.find_paths(a.id, b.id, max_hops=2)
        assert result["paths"] == []

    def test_nonexistent_source_id_raises_does_not_exist(self):
        """GIVEN a source_id with no matching row WHEN find_paths is called
        THEN Entity.DoesNotExist is raised."""
        target = Entity.objects.create(entity_type="company", name="Target Ltd")
        with pytest.raises(Entity.DoesNotExist):
            tools.find_paths(999999, target.id)

    def test_nonexistent_target_id_raises_does_not_exist(self):
        """GIVEN a target_id with no matching row WHEN find_paths is called
        THEN Entity.DoesNotExist is raised."""
        source = Entity.objects.create(entity_type="person", name="Source MP")
        with pytest.raises(Entity.DoesNotExist):
            tools.find_paths(source.id, 999999)

    def test_max_hops_within_range_is_echoed_as_the_effective_value(self):
        """GIVEN a max_hops within the allowed range WHEN find_paths is
        called THEN the response's max_hops key equals what was requested."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="company", name="B")
        result = tools.find_paths(a.id, b.id, max_hops=2)
        assert result["max_hops"] == 2

    def test_oversized_max_hops_is_clamped_to_the_server_side_ceiling(self):
        """GIVEN a caller-supplied max_hops far beyond the server-side
        ceiling WHEN find_paths is called THEN the response's max_hops key
        reports the CLAMPED effective value, not the raw requested value --
        the clamp is silent-proof, not silent."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="company", name="B")
        result = tools.find_paths(a.id, b.id, max_hops=1_000_000)
        assert result["max_hops"] == tools._MAX_FIND_PATHS_HOPS

    def test_oversized_max_hops_walk_actually_uses_the_clamped_value(self, monkeypatch):
        """GIVEN a caller-supplied max_hops beyond the ceiling WHEN
        find_paths is called THEN the underlying traversal is invoked with
        the CLAMPED value, not the raw one -- proves the clamp is enforced
        before the walk runs, not just echoed in the response."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="company", name="B")
        captured = {}

        def spy(start_ids, goal_id, adj, max_hops, cutoff):
            captured["max_hops"] = max_hops
            return [], []

        monkeypatch.setattr(tools, "_phase_c_find_paths", spy)
        tools.find_paths(a.id, b.id, max_hops=1_000_000)
        assert captured["max_hops"] == tools._MAX_FIND_PATHS_HOPS

    def test_zero_or_negative_max_hops_is_clamped_up_to_one(self):
        """GIVEN a caller-supplied max_hops of zero WHEN find_paths is
        called THEN the effective max_hops is clamped up to 1, not 0."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="company", name="B")
        result = tools.find_paths(a.id, b.id, max_hops=0)
        assert result["max_hops"] == 1


@pytest.mark.django_db
class TestGetAttestations:
    def test_returns_every_attestation_on_the_edge(self):
        """GIVEN an edge with two attestations WHEN get_attestations is
        called THEN both are returned with their source citations."""
        donor = Entity.objects.create(entity_type="person", name="Donor")
        recipient = Entity.objects.create(entity_type="company", name="Recipient Ltd")
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=donor,
            target_entity=recipient,
            valid_from="2020-04-01",
            amount_cents=5000000,
            currency="GBP",
        )
        Attestation.objects.create(
            edge=edge,
            source_name="Electoral Commission",
            source_url="https://example.com/ec/1",
            source_reference="EC-1",
            snapshot_ref="a" * 64,
        )
        Attestation.objects.create(edge=edge, source_name="Companies House")

        result = tools.get_attestations(edge.id)
        assert result["edge_type"] == "donation"
        assert len(result["attestations"]) == 2
        source_names = {a["source_name"] for a in result["attestations"]}
        assert source_names == {"Electoral Commission", "Companies House"}

    def test_attestation_carries_source_url_and_snapshot_ref(self):
        """GIVEN an attestation with a source_url and snapshot_ref WHEN
        get_attestations is called THEN both fields round-trip exactly."""
        donor = Entity.objects.create(entity_type="person", name="Donor")
        recipient = Entity.objects.create(entity_type="company", name="Recipient Ltd")
        edge = Edge.objects.create(
            edge_type="donation", source_entity=donor, target_entity=recipient
        )
        Attestation.objects.create(
            edge=edge,
            source_name="Electoral Commission",
            source_url="https://example.com/ec/1",
            snapshot_ref="b" * 64,
        )
        result = tools.get_attestations(edge.id)
        [attestation] = result["attestations"]
        assert attestation["source_url"] == "https://example.com/ec/1"
        assert attestation["snapshot_ref"] == "b" * 64

    def test_source_entity_summary_is_privacy_filtered(self):
        """GIVEN an edge whose source entity is a person with personal
        fields stuffed into properties WHEN get_attestations is called THEN
        the returned source_entity carries no properties key."""
        donor = Entity.objects.create(
            entity_type="person",
            name="Donor",
            properties={"date_of_birth": "1980-01-01"},
        )
        recipient = Entity.objects.create(entity_type="company", name="Recipient Ltd")
        edge = Edge.objects.create(
            edge_type="donation", source_entity=donor, target_entity=recipient
        )
        Attestation.objects.create(edge=edge, source_name="Electoral Commission")

        result = tools.get_attestations(edge.id)
        assert "properties" not in result["source_entity"]

    def test_nonexistent_edge_id_raises_does_not_exist(self):
        """GIVEN an edge_id with no matching row WHEN get_attestations is
        called THEN Edge.DoesNotExist is raised."""
        with pytest.raises(Edge.DoesNotExist):
            tools.get_attestations(999999)


@pytest.mark.django_db
class TestCoverageReport:
    def test_default_universe_delegates_to_ch_officers_coverage_report(self, monkeypatch):
        """GIVEN universe="all" (the default) WHEN coverage_report is
        called THEN it returns exactly what ch_officers.coverage_report()
        returns -- no coverage logic is reimplemented here."""
        sentinel = {"total_gb_coh_companies": 42}
        monkeypatch.setattr(ch_officers, "coverage_report", lambda: sentinel)
        assert tools.coverage_report() == sentinel

    def test_procurement_supplier_universe_delegates_to_procurement_report(self, monkeypatch):
        """GIVEN universe="procurement_supplier" WHEN coverage_report is
        called THEN it returns exactly what
        ch_officers.procurement_universe_coverage_report() returns."""
        sentinel = {"universe_size": 7}
        monkeypatch.setattr(ch_officers, "procurement_universe_coverage_report", lambda: sentinel)
        assert tools.coverage_report(universe="procurement_supplier") == sentinel

    def test_unknown_universe_raises_value_error(self):
        """GIVEN an unrecognised universe name WHEN coverage_report is
        called THEN it raises ValueError."""
        with pytest.raises(ValueError):
            tools.coverage_report(universe="not_a_real_universe")


class TestListSources:
    def test_returns_one_entry_per_register_file_with_source_id(self):
        """GIVEN the sources/ register on disk WHEN list_sources is called
        THEN every returned entry carries the source_id field and the count
        matches the register loader's own count."""
        from uncorrupt.register.loader import all_sources

        expected_ids = {s.source_id for s in all_sources()}
        results = tools.list_sources()
        assert {r["source_id"] for r in results} == expected_ids

    def test_entries_never_expose_a_dpia_cleared_true_without_the_field_present(self):
        """GIVEN the register WHEN list_sources is called THEN every entry
        carries a boolean dpia_cleared field (never silently omitted)."""
        results = tools.list_sources()
        assert all(isinstance(r["dpia_cleared"], bool) for r in results)


class TestDescribePipeline:
    def test_returns_the_verbatim_readme_contents(self):
        """GIVEN sources/README.md on disk WHEN describe_pipeline is called
        THEN the returned text is byte-identical to the file's contents --
        it cannot drift from the documented contract."""
        on_disk = tools._README_PATH.read_text(encoding="utf-8")
        assert tools.describe_pipeline() == on_disk
