"""Tests for `scripts.run_gold_benchmark.compute_graph_hash`'s None/date sort fix.

Real `officer_of` reappointment pairs share (edge_type, source_entity_id,
target_entity_id) with one row's `valid_from` `None` and the other's a
`datetime.date` -- sorting the raw tuples directly raised `TypeError: '<' not
supported between instances of 'NoneType' and 'datetime.date'`, which blocked
`measure_coverage_gate.py`/`measure_stratum_gates.py` end-to-end. These tests
cover the delegation packet's required scenarios: no crash on a mixed
None/dated graph, order-independence, and that undated edges are still
counted (never silently dropped from the hash).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

import pytest
from scripts.run_gold_benchmark import compute_graph_hash

from uncorrupt.graph.models import Edge, Entity


@pytest.mark.django_db
class TestComputeGraphHashHandlesMixedNoneAndDatedValidFrom:
    def test_does_not_raise_when_edges_share_type_source_target_with_mixed_valid_from(self):
        """GIVEN two officer_of edges between the SAME officer and company --
        one with valid_from=None (an unresolved appointment date) and one with
        a real date (a reappointment) -- the exact shape of a real Companies
        House reappointment pair
        WHEN compute_graph_hash is called
        THEN it returns a hash string instead of raising TypeError."""
        officer = Entity.objects.create(entity_type="person", name="Someone")
        company = Entity.objects.create(entity_type="company", name="Somewhere Ltd")
        Edge.objects.create(
            edge_type="officer_of", source_entity=officer, target_entity=company, valid_from=None
        )
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=officer,
            target_entity=company,
            valid_from=date(2020, 1, 1),
        )

        result = compute_graph_hash()

        assert isinstance(result, str)
        assert len(result) == 64  # sha256 hex digest

    def test_undated_edges_are_not_dropped_from_the_hash(self):
        """GIVEN a graph with one dated edge, hashed
        WHEN a second, UNDATED edge (valid_from=None) is added between a new
        officer/company pair
        THEN the hash changes -- an undated-only change must be visible in the
        hash, never silently absent from it."""
        officer_a = Entity.objects.create(entity_type="person", name="Officer A")
        company_a = Entity.objects.create(entity_type="company", name="Company A")
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=officer_a,
            target_entity=company_a,
            valid_from=date(2019, 6, 1),
        )
        hash_before = compute_graph_hash()

        officer_b = Entity.objects.create(entity_type="person", name="Officer B")
        company_b = Entity.objects.create(entity_type="company", name="Company B")
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=officer_b,
            target_entity=company_b,
            valid_from=None,
        )
        hash_after = compute_graph_hash()

        assert hash_after != hash_before


class TestComputeGraphHashIsOrderIndependent:
    def test_same_hash_from_a_shuffled_queryset(self):
        """GIVEN the exact same set of edge rows -- one with valid_from=None,
        two with dated valid_from -- returned by the DB query in two different
        orders (forward and reversed, i.e. a shuffled queryset)
        WHEN compute_graph_hash is called against each ordering
        THEN both calls produce the identical hash -- insertion/fetch order
        must never change a freeze-state identity hash."""
        rows = [
            ("officer_of", 1, 2, None),
            ("officer_of", 1, 2, date(2020, 1, 1)),
            ("declared_interest", 3, 4, date(2019, 5, 5)),
        ]

        with patch("scripts.run_gold_benchmark.Edge.objects.values_list", return_value=list(rows)):
            forward_hash = compute_graph_hash()

        with patch(
            "scripts.run_gold_benchmark.Edge.objects.values_list",
            return_value=list(reversed(rows)),
        ):
            reversed_hash = compute_graph_hash()

        assert forward_hash == reversed_hash
