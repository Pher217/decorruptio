"""Tests for `scripts/phase_c_paths.py`'s path-report serialization.

Covers `_serialize_paths`, the helper that renders a set of paths for the
JSON report and attaches the non-gating `min_identity_confidence` diagnostic
(see `register_snapshots.path_min_identity_confidence`) index-aligned with
each rendered path. This is strictly additive, exploratory metadata — it
must never affect `status`, path selection, or any counted outcome, only
what gets reported alongside a path.
"""

from __future__ import annotations

from datetime import date

import pytest
from scripts.phase_c_paths import _serialize_paths

from uncorrupt.graph.models import Attestation, Edge, Entity


@pytest.mark.django_db
class TestSerializePaths:
    def test_empty_path_list_renders_empty_lists(self):
        """No paths in, nothing rendered, nothing to report a confidence for."""
        rendered, confidences = _serialize_paths([])

        assert rendered == []
        assert confidences == []

    def test_path_with_no_identity_bridge_has_none_confidence(self):
        """A path with no `same_as` edge renders normally, with `None` (not
        a number) in the parallel confidence list."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="company", name="B")
        dated_edge = Edge.objects.create(
            edge_type="officer_of", source_entity=a, target_entity=b, valid_from=date(2015, 1, 1)
        )

        rendered, confidences = _serialize_paths([[dated_edge]])

        assert rendered == [[f"officer_of@{date(2015, 1, 1)}"]]
        assert confidences == [None]

    def test_path_with_identity_bridge_reports_its_confidence(self):
        """A path bridged by a `same_as` edge carries that edge's
        attestation confidence in the parallel list, at the same index."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="person", name="A (CH record)")
        c = Entity.objects.create(entity_type="company", name="C")
        same_as_edge = Edge.objects.create(edge_type="same_as", source_entity=a, target_entity=b)
        Attestation.objects.create(
            edge=same_as_edge,
            source_name="Cross-register identity resolution",
            match_confidence=0.60,
        )
        dated_edge = Edge.objects.create(
            edge_type="officer_of", source_entity=b, target_entity=c, valid_from=date(2015, 1, 1)
        )

        rendered, confidences = _serialize_paths([[same_as_edge, dated_edge]])

        assert len(rendered) == 1
        assert confidences == [0.60]

    def test_confidences_are_index_aligned_across_multiple_paths(self):
        """With several paths, the Nth confidence describes the Nth
        rendered path, not an arbitrary or sorted ordering."""
        a = Entity.objects.create(entity_type="person", name="A")
        b = Entity.objects.create(entity_type="person", name="A (CH record)")
        c = Entity.objects.create(entity_type="company", name="C")
        d = Entity.objects.create(entity_type="company", name="D")

        no_bridge_edge = Edge.objects.create(
            edge_type="officer_of", source_entity=a, target_entity=c, valid_from=date(2012, 1, 1)
        )

        same_as_edge = Edge.objects.create(edge_type="same_as", source_entity=a, target_entity=b)
        Attestation.objects.create(
            edge=same_as_edge,
            source_name="Cross-register identity resolution",
            match_confidence=0.85,
        )
        bridged_edge = Edge.objects.create(
            edge_type="officer_of", source_entity=b, target_entity=d, valid_from=date(2013, 1, 1)
        )

        rendered, confidences = _serialize_paths([[no_bridge_edge], [same_as_edge, bridged_edge]])

        assert len(rendered) == 2
        assert confidences == [None, 0.85]

    def test_truncates_to_the_first_five_paths(self):
        """More than 5 paths are truncated, matching the existing
        `pre_award_paths`/`undated_paths` cap — the confidence list is
        truncated identically so the two stay index-aligned."""
        a = Entity.objects.create(entity_type="person", name="A")
        companies = [Entity.objects.create(entity_type="company", name=f"C{i}") for i in range(7)]
        paths = [
            [
                Edge.objects.create(
                    edge_type="officer_of",
                    source_entity=a,
                    target_entity=company,
                    valid_from=date(2010, 1, 1),
                )
            ]
            for company in companies
        ]

        rendered, confidences = _serialize_paths(paths)

        assert len(rendered) == 5
        assert len(confidences) == 5
