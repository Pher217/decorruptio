"""Tests for the Lords-stratum retrieval control runner.

`scripts/run_lords_controls.py` closes the per-stratum control-battery gap
required by amendment v2.4: the House of Lords had no control because
`parliament.uk` gated plain fetches (Cloudflare bot-challenge). A frozen,
hashed snapshot fixed that at the source-retrieval layer; this runner tests
whether the pipeline can recover each of the 12 register-visible
peer<->company relationships from the graph.

These tests verify the discipline the packet requires:
- a present relationship is recovered
- an absent company is classified `not-found`, never `unresolved` (absence
  is a data-coverage gap, not a retrieval-logic failure)
- an ambiguous (2+) company match IS `unresolved` (a real resolution-logic
  limitation, correctly not guessed)
- an unresolved placeholder node (`UK-LORDS-UNRESOLVED`) never counts as a
  resolved company -- it is a weak, per-interest scoped node, not a
  verifiable registry entity
- selection from the fixture is deterministic across repeated loads/runs
- the output can never report a temporal pass for this stratum, regardless
  of row content
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest
from scripts.phase_c_paths import build_adjacency, surname
from scripts.run_lords_controls import (
    DEFAULT_CONTROLS_PATH,
    ENDPOINT,
    TEMPORAL_ENDPOINT_STATUS,
    classify_control,
    load_controls,
    resolve_company_candidates,
    run_controls,
)

from uncorrupt.graph.models import Edge, Entity


def _write_controls(tmp_path: Path, controls: list[dict]) -> Path:
    path = tmp_path / "controls.json"
    path.write_text(json.dumps({"controls": controls}), encoding="utf-8")
    return path


def _people_by_surname(*entities: Entity) -> dict[str, list[Entity]]:
    index: dict[str, list[Entity]] = defaultdict(list)
    for entity in entities:
        sn = surname(entity.name)
        if sn:
            index[sn].append(entity)
    return index


class TestLoadControlsIsDeterministic:
    def test_load_controls_returns_the_full_fixed_battery(self):
        """GIVEN the pre-registered 12-row control fixture
        WHEN it is loaded
        THEN all 12 rows are returned, in file order."""
        controls = load_controls(DEFAULT_CONTROLS_PATH)
        assert len(controls) == 12
        assert [c["id"] for c in controls] == list(range(1, 13))

    def test_load_controls_is_deterministic_across_repeated_calls(self):
        """GIVEN the same fixture path
        WHEN loaded twice
        THEN the ordered id sequence is identical both times -- nothing here
        samples, shuffles, or reselects to flatter a result."""
        first = load_controls(DEFAULT_CONTROLS_PATH)
        second = load_controls(DEFAULT_CONTROLS_PATH)
        assert [c["id"] for c in first] == [c["id"] for c in second]

    def test_load_controls_from_custom_fixture_preserves_order(self, tmp_path):
        """GIVEN a custom fixture with rows in a specific order
        WHEN loaded
        THEN the returned list matches that order exactly, unreordered."""
        controls = [
            {"id": 3, "peer_name": "Lord C", "declared_company": "C Ltd"},
            {"id": 1, "peer_name": "Lord A", "declared_company": "A Ltd"},
            {"id": 2, "peer_name": "Lord B", "declared_company": "B Ltd"},
        ]
        path = _write_controls(tmp_path, controls)

        loaded = load_controls(path)

        assert [c["id"] for c in loaded] == [3, 1, 2]


@pytest.mark.django_db
class TestClassifyControlRecovered:
    def test_present_relationship_is_recovered(self):
        """GIVEN a peer and a company connected by a declared_interest edge
        WHEN the control is classified
        THEN the status is 'recovered' and a path is reported."""
        company = Entity.objects.create(
            entity_type="company",
            name="Acme Widgets Ltd",
            registry_scheme="GB-COH",
            registry_id="01234567",
            company_number="01234567",
        )
        peer = Entity.objects.create(
            entity_type="person",
            name="Lord Testington",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="9999",
        )
        Edge.objects.create(
            edge_type="declared_interest", source_entity=peer, target_entity=company
        )
        control = {
            "id": 1,
            "page": 1,
            "member_id": "9999",
            "peer_name": "Lord Testington",
            "declared_company": "Acme Widgets Ltd",
        }
        people_by_surname = _people_by_surname(peer)
        adj = build_adjacency()

        row = classify_control(control, people_by_surname, adj, max_hops=2)

        assert row["status"] == "recovered"
        assert row["path_count"] >= 1
        assert row["resolved_company"] == "Acme Widgets Ltd"

    def test_titled_peer_name_resolves_via_surname_not_exact_string(self):
        """GIVEN a peer stored with a territorial designation ("of X")
        WHEN the register wording is classified
        THEN it still resolves -- the same weak, external-table-style
        matching every other control must survive."""
        company = Entity.objects.create(
            entity_type="company",
            name="Oulton Holdings Ltd",
            registry_scheme="GB-COH",
            registry_id="00000042",
            company_number="00000042",
        )
        peer = Entity.objects.create(
            entity_type="person",
            name="Lord Agnew of Oulton",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="4242",
        )
        Edge.objects.create(
            edge_type="declared_interest", source_entity=peer, target_entity=company
        )
        control = {
            "id": 1,
            "page": 1,
            "member_id": "4242",
            "peer_name": "Lord Agnew of Oulton",
            "declared_company": "Oulton Holdings Ltd",
        }
        people_by_surname = _people_by_surname(peer)
        adj = build_adjacency()

        row = classify_control(control, people_by_surname, adj, max_hops=2)

        assert row["status"] == "recovered"


@pytest.mark.django_db
class TestClassifyControlNotFound:
    def test_absent_company_is_not_found_not_unresolved(self):
        """GIVEN a peer that exists but a declared company with no matching
        company-entity anywhere in the graph
        WHEN classified
        THEN status is 'not-found', never 'unresolved' -- absence is a
        data-coverage gap, not a retrieval-logic failure."""
        peer = Entity.objects.create(
            entity_type="person",
            name="Lord Testington",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="9999",
        )
        control = {
            "id": 2,
            "page": 1,
            "member_id": "9999",
            "peer_name": "Lord Testington",
            "declared_company": "Nonexistent Company Ltd",
        }
        people_by_surname = _people_by_surname(peer)
        adj = build_adjacency()

        row = classify_control(control, people_by_surname, adj, max_hops=2)

        assert row["status"] == "not-found"
        assert row["status"] != "unresolved"

    def test_absent_peer_is_not_found(self):
        """GIVEN a company that exists but no person-entity for the peer at all
        WHEN classified
        THEN status is 'not-found'."""
        Entity.objects.create(
            entity_type="company",
            name="Acme Widgets Ltd",
            registry_scheme="GB-COH",
            registry_id="01234567",
            company_number="01234567",
        )
        control = {
            "id": 3,
            "page": 1,
            "member_id": "1234",
            "peer_name": "Lord Nobody",
            "declared_company": "Acme Widgets Ltd",
        }
        people_by_surname: dict[str, list[Entity]] = {}
        adj = build_adjacency()

        row = classify_control(control, people_by_surname, adj, max_hops=2)

        assert row["status"] == "not-found"

    def test_resolved_both_uniquely_but_no_path_is_not_found(self):
        """GIVEN a peer and a company that both resolve uniquely but share no
        connecting edge
        WHEN classified
        THEN status is 'not-found' with a zero path count."""
        peer = Entity.objects.create(
            entity_type="person",
            name="Lord Testington",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="9999",
        )
        Entity.objects.create(
            entity_type="company",
            name="Acme Widgets Ltd",
            registry_scheme="GB-COH",
            registry_id="01234567",
            company_number="01234567",
        )
        control = {
            "id": 4,
            "page": 1,
            "member_id": "9999",
            "peer_name": "Lord Testington",
            "declared_company": "Acme Widgets Ltd",
        }
        people_by_surname = _people_by_surname(peer)
        adj = build_adjacency()

        row = classify_control(control, people_by_surname, adj, max_hops=2)

        assert row["status"] == "not-found"
        assert row["path_count"] == 0

    def test_unresolved_placeholder_entity_is_not_a_company_match(self):
        """GIVEN a Lords interest already ingested to a UK-LORDS-UNRESOLVED
        regulated_entity placeholder (not a real company registry node)
        WHEN resolve_company_candidates searches for that same declared name
        THEN it returns no candidates -- a weak, per-interest scoped
        placeholder must never masquerade as a retrieved company link."""
        Entity.objects.create(
            entity_type="regulated_entity",
            name="Microlink PC (UK) Ltd",
            registry_scheme="UK-LORDS-UNRESOLVED",
            registry_id="scopedkey123",
        )

        candidates = resolve_company_candidates("Microlink PC (UK) Ltd")

        assert candidates == []


@pytest.mark.django_db
class TestClassifyControlUnresolved:
    def test_ambiguous_company_match_is_unresolved(self):
        """GIVEN two distinct GB-COH company entities with the identical
        normalised name
        WHEN classified
        THEN status is 'unresolved' -- the pipeline correctly refuses to
        guess which one is right (ADR-006 discipline)."""
        peer = Entity.objects.create(
            entity_type="person",
            name="Lord Testington",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="9999",
        )
        Entity.objects.create(
            entity_type="company",
            name="Acme Ltd",
            registry_scheme="GB-COH",
            registry_id="11111111",
            company_number="11111111",
        )
        Entity.objects.create(
            entity_type="company",
            name="Acme Ltd",
            registry_scheme="GB-COH",
            registry_id="22222222",
            company_number="22222222",
        )
        control = {
            "id": 5,
            "page": 1,
            "member_id": "9999",
            "peer_name": "Lord Testington",
            "declared_company": "Acme Ltd",
        }
        people_by_surname = _people_by_surname(peer)
        adj = build_adjacency()

        row = classify_control(control, people_by_surname, adj, max_hops=2)

        assert row["status"] == "unresolved"


class TestNeverReportsATemporalPass:
    def test_fixed_constants_never_claim_a_temporal_endpoint(self):
        """GIVEN the module's fixed scope constants
        WHEN read directly
        THEN the endpoint is always 'retrieval' and the temporal status is
        always 'INSTRUMENT-LIMITED' -- these are constants, not computed
        from row outcomes, so no row content can ever flip them."""
        assert ENDPOINT == "retrieval"
        assert TEMPORAL_ENDPOINT_STATUS == "INSTRUMENT-LIMITED"

    @pytest.mark.django_db
    def test_output_reports_fixed_temporal_status_even_when_every_row_recovers(self, tmp_path):
        """GIVEN a control battery where the sole row fully recovers, INCLUDING
        a dated edge that could tempt a temporal read
        WHEN run_controls executes
        THEN the top-level temporal_endpoint_status is still
        'INSTRUMENT-LIMITED' and no temporal-sounding key (pre_award,
        temporal_pass) appears anywhere in the output."""
        company = Entity.objects.create(
            entity_type="company",
            name="Acme Widgets Ltd",
            registry_scheme="GB-COH",
            registry_id="01234567",
            company_number="01234567",
        )
        peer = Entity.objects.create(
            entity_type="person",
            name="Lord Testington",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="9999",
        )
        # A dated edge -- the strongest possible temptation to leak a
        # temporal signal. It must still never surface as one.
        Edge.objects.create(
            edge_type="declared_interest",
            source_entity=peer,
            target_entity=company,
            valid_from=date(2015, 1, 1),
        )
        controls = [
            {
                "id": 1,
                "page": 1,
                "member_id": "9999",
                "peer_name": "Lord Testington",
                "declared_company": "Acme Widgets Ltd",
            }
        ]
        path = _write_controls(tmp_path, controls)

        result = run_controls(path, max_hops=2)

        assert result["recovered"] == 1
        assert result["endpoint"] == "retrieval"
        assert result["temporal_endpoint_status"] == "INSTRUMENT-LIMITED"
        assert "temporal_pass" not in result
        assert "pre_award" not in result
        for row in result["rows"]:
            assert "pre_award_path_count" not in row
            assert "temporal_pass" not in row


@pytest.mark.django_db
class TestRunControlsDeterminism:
    def test_run_controls_is_deterministic_across_repeated_runs(self, tmp_path):
        """GIVEN a fixed control list and unchanged graph state
        WHEN run_controls executes twice in a row
        THEN both runs produce the identical per-row status sequence and
        summary counts."""
        company = Entity.objects.create(
            entity_type="company",
            name="Acme Widgets Ltd",
            registry_scheme="GB-COH",
            registry_id="01234567",
            company_number="01234567",
        )
        peer = Entity.objects.create(
            entity_type="person",
            name="Lord Testington",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="9999",
        )
        Edge.objects.create(
            edge_type="declared_interest", source_entity=peer, target_entity=company
        )
        controls = [
            {
                "id": 1,
                "page": 1,
                "member_id": "9999",
                "peer_name": "Lord Testington",
                "declared_company": "Acme Widgets Ltd",
            },
            {
                "id": 2,
                "page": 1,
                "member_id": "0000",
                "peer_name": "Lord Nobody",
                "declared_company": "Nonexistent Ltd",
            },
        ]
        path = _write_controls(tmp_path, controls)

        first = run_controls(path, max_hops=2)
        second = run_controls(path, max_hops=2)

        assert [r["status"] for r in first["rows"]] == [r["status"] for r in second["rows"]]
        assert (first["recovered"], first["unresolved"], first["not_found"]) == (
            second["recovered"],
            second["unresolved"],
            second["not_found"],
        )
        assert first["recovered"] == 1
        assert first["not_found"] == 1


@pytest.mark.django_db
class TestRunControlsAgainstEmptyGraph:
    def test_full_fixture_against_empty_graph_is_all_not_found_and_does_not_crash(self):
        """GIVEN the real 12-row production fixture and an empty test graph
        WHEN run_controls executes
        THEN it completes without error and every row is 'not-found' (no
        matching data exists in this test database) -- never
        'unresolved', since nothing here is ambiguous, just absent."""
        result = run_controls(DEFAULT_CONTROLS_PATH, max_hops=2)

        assert result["n"] == 12
        assert result["not_found"] == 12
        assert result["recovered"] == 0
        assert result["unresolved"] == 0
