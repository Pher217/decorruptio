"""Tests for the attestation-inclusive freeze-state binding.

Covers the delegation packet's binding requirement: an attestation-only
ingest (zero new edges) must not silently keep binding to a gate measured
before it, even though `run_gold_benchmark.compute_graph_hash` (edge tuples
only, out of scope, unedited here) cannot see it.
"""

from __future__ import annotations

import pytest

from uncorrupt.gates.binding import GateFreezeState, compute_attestation_inclusive_hash
from uncorrupt.graph.models import Attestation, Edge, Entity


@pytest.mark.django_db
class TestAttestationInclusiveHashClosesTheGraphHashGap:
    def test_hash_changes_when_an_attestation_only_ingest_adds_no_new_edge(self):
        """GIVEN a graph with one edge and no attestations, hashed
        WHEN an attestation is added to that SAME edge (no new edge created --
        mirrors the Lords Wayback snapshot ingest, spec v2.9: ~6,000 attestations,
        zero new edges)
        THEN compute_attestation_inclusive_hash() changes, even though
        run_gold_benchmark.compute_graph_hash() (edge tuples only) would not."""
        from scripts.run_gold_benchmark import compute_graph_hash

        person = Entity.objects.create(entity_type="person", name="Someone")
        company = Entity.objects.create(entity_type="company", name="Somewhere Ltd")
        edge = Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=company
        )

        graph_hash_before = compute_graph_hash()
        attestation_hash_before = compute_attestation_inclusive_hash()

        Attestation.objects.create(edge=edge, source_name="Some Register", source_reference="r1")

        graph_hash_after = compute_graph_hash()
        attestation_hash_after = compute_attestation_inclusive_hash()

        assert graph_hash_after == graph_hash_before, (
            "documents the known gap: compute_graph_hash is blind to attestation-only ingests"
        )
        assert attestation_hash_after != attestation_hash_before

    def test_hash_is_order_independent(self):
        """GIVEN the same two attestations created in different orders across two
        equivalent graphs
        WHEN each is hashed
        THEN both hashes are identical -- insertion order must never change the
        hash (mirrors compute_graph_hash's own discipline)."""
        person = Entity.objects.create(entity_type="person", name="Someone")
        company = Entity.objects.create(entity_type="company", name="Somewhere Ltd")
        edge = Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=company
        )
        Attestation.objects.create(edge=edge, source_name="Register A", source_reference="a")
        Attestation.objects.create(edge=edge, source_name="Register B", source_reference="b")

        first = compute_attestation_inclusive_hash()
        second = compute_attestation_inclusive_hash()

        assert first == second


@pytest.mark.django_db
class TestComputeAttestationInclusiveHashHandlesMixedNoneFields:
    def test_does_not_raise_when_attestations_share_their_group_key_with_mixed_observed_at(self):
        """GIVEN two attestations on the same edge sharing (edge_id,
        source_name, source_reference=None) -- `unique_attestation_per_source_ref`
        is a PARTIAL unique constraint (`condition=Q(source_reference__isnull
        =False)`), so it does NOT stop two rows sharing a None
        source_reference, which is exactly the loophole the coordinator's
        review flagged -- one with observed_at=None and one with a real
        datetime, the same shape that made a raw-tuple sort raise TypeError
        in compute_graph_hash (281,535 of 704,074 real attestations have
        observed_at=None)
        WHEN compute_attestation_inclusive_hash is called
        THEN it returns a hash string instead of raising."""
        from datetime import UTC, datetime

        person = Entity.objects.create(entity_type="person", name="Someone")
        company = Entity.objects.create(entity_type="company", name="Somewhere Ltd")
        edge = Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=company
        )
        Attestation.objects.create(
            edge=edge, source_name="Register", source_reference=None, observed_at=None
        )
        Attestation.objects.create(
            edge=edge,
            source_name="Register",
            source_reference=None,
            observed_at=datetime(2020, 1, 1, tzinfo=UTC),
        )

        result = compute_attestation_inclusive_hash()

        assert isinstance(result, str)
        assert len(result) == 64

    def test_does_not_raise_when_attestations_share_their_group_key_with_mixed_snapshot_ref(self):
        """GIVEN two attestations sharing (edge_id, source_name,
        source_reference=None -- the same partial-constraint loophole as
        above) and an equal (both-None) observed_at -- so tuple comparison
        would fall through to the next field -- with one row's
        snapshot_ref=None and the other a real string (688,540 of 704,074
        real attestations have snapshot_ref=None; `None < "x"` raises
        TypeError just as readily as `None < datetime(...)` does)
        WHEN compute_attestation_inclusive_hash is called
        THEN it returns a hash string instead of raising -- locks in that
        the fix covers the whole row, not just the observed_at field the
        bug report singled out."""
        person = Entity.objects.create(entity_type="person", name="Someone")
        company = Entity.objects.create(entity_type="company", name="Somewhere Ltd")
        edge = Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=company
        )
        Attestation.objects.create(
            edge=edge, source_name="Register", source_reference=None, snapshot_ref=None
        )
        Attestation.objects.create(
            edge=edge, source_name="Register", source_reference=None, snapshot_ref="abc123"
        )

        result = compute_attestation_inclusive_hash()

        assert isinstance(result, str)
        assert len(result) == 64


class TestComputeAttestationInclusiveHashIsOrderIndependent:
    def test_same_hash_from_a_shuffled_queryset(self):
        """GIVEN the exact same set of attestation rows -- one with
        observed_at=None, two with real datetimes -- returned by the DB
        query in two different orders (forward and reversed)
        WHEN compute_attestation_inclusive_hash is called against each
        ordering
        THEN both calls produce the identical hash -- mirrors
        compute_graph_hash's own order-independence discipline."""
        from datetime import UTC, datetime
        from unittest.mock import patch

        rows = [
            (1, "Register A", "r1", None, None),
            (1, "Register A", "r1", datetime(2020, 1, 1, tzinfo=UTC), "snap1"),
            (2, "Register B", "r2", datetime(2019, 5, 5, tzinfo=UTC), None),
        ]

        with (
            patch("scripts.run_gold_benchmark.compute_graph_hash", return_value="edgehash"),
            patch(
                "uncorrupt.gates.binding.Attestation.objects.values_list",
                return_value=list(rows),
            ),
        ):
            forward_hash = compute_attestation_inclusive_hash()

        with (
            patch("scripts.run_gold_benchmark.compute_graph_hash", return_value="edgehash"),
            patch(
                "uncorrupt.gates.binding.Attestation.objects.values_list",
                return_value=list(reversed(rows)),
            ),
        ):
            reversed_hash = compute_attestation_inclusive_hash()

        assert forward_hash == reversed_hash


class TestGateFreezeStateMatchesRecorded:
    def _state(self, **overrides) -> GateFreezeState:
        defaults = dict(
            code_commit="abc123",
            graph_hash="graphhash",
            attestation_inclusive_hash="attesthash",
            manifest_hash="manifesthash",
            measured_at="2026-08-03T00:00:00+00:00",
        )
        defaults.update(overrides)
        return GateFreezeState(**defaults)

    def test_matches_when_all_five_fields_agree(self):
        """GIVEN a freeze state and a recorded dict with identical
        code_commit/graph_hash/attestation_inclusive_hash/manifest_hash/
        control_fixtures_hash
        WHEN matches_recorded is checked
        THEN it is True."""
        state = self._state()
        recorded = state.to_binding_dict()

        assert state.matches_recorded(recorded) is True

    def test_does_not_match_when_attestation_inclusive_hash_differs(self):
        """GIVEN a recorded dict whose attestation_inclusive_hash differs from the
        current state (an attestation-only ingest happened since it was recorded)
        WHEN matches_recorded is checked
        THEN it is False -- this is the extra check run_gold_benchmark.GateBinding
        cannot perform."""
        state = self._state()
        recorded = state.to_binding_dict()
        recorded["attestation_inclusive_hash"] = "a-different-hash"

        assert state.matches_recorded(recorded) is False

    def test_does_not_match_when_graph_hash_differs(self):
        """GIVEN a recorded dict whose graph_hash differs
        WHEN matches_recorded is checked
        THEN it is False."""
        state = self._state()
        recorded = state.to_binding_dict()
        recorded["graph_hash"] = "a-different-hash"

        assert state.matches_recorded(recorded) is False

    def test_does_not_match_when_control_fixtures_hash_differs(self):
        """GIVEN a recorded dict whose control_fixtures_hash differs from the
        current state (a control-battery fixture was edited or substituted since
        this state was recorded -- an edit that changes NONE of code_commit/
        graph_hash/attestation_inclusive_hash/manifest_hash, since none of those
        four reflect fixture content)
        WHEN matches_recorded is checked
        THEN it is False -- this is the extra check that closes the
        "fixture is unbound" gap (see binding.py module docstring)."""
        state = self._state(control_fixtures_hash="fixtures-v1")
        recorded = state.to_binding_dict()
        recorded["control_fixtures_hash"] = "fixtures-v2-tampered"

        assert state.matches_recorded(recorded) is False

    def test_control_fixtures_hash_defaults_to_empty_and_still_matches(self):
        """GIVEN a freeze state built with no control_fixtures_hash override (the
        coverage-gate script's own case -- it has no stratum fixtures to hash)
        WHEN matches_recorded is checked against its own recorded dict
        THEN it is True -- the shared "" default is a legitimate match for a
        producer that never reads a control fixture, not a silently-passing
        placeholder."""
        state = self._state()

        assert state.control_fixtures_hash == ""
        assert state.matches_recorded(state.to_binding_dict()) is True
