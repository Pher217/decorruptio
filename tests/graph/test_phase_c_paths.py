"""Tests for `scripts/phase_c_paths.py`'s path-report serialization.

Covers `_serialize_paths`, the helper that renders a set of paths for the
JSON report and attaches the non-gating `min_identity_confidence` diagnostic
(see `register_snapshots.path_min_identity_confidence`) index-aligned with
each rendered path. This is strictly additive, exploratory metadata — it
must never affect `status`, path selection, or any counted outcome, only
what gets reported alongside a path.
"""

from __future__ import annotations

import json
import sys
from datetime import date

import pytest
from scripts import phase_c_paths
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


@pytest.mark.django_db
class TestMainNonGating:
    """`_serialize_paths` attaches a purely additive, non-gating
    `min_identity_confidence` diagnostic to each rendered path (see its own
    docstring and `register_snapshots.path_min_identity_confidence`'s).
    This pins that claim empirically instead of resting on it as a comment:
    running `main()` end-to-end with the REAL confidence function, and again
    with two stubs pinned at the OPPOSITE ends of the confidence range (0.0
    and 1.0), must produce IDENTICAL `status` (per row) and `counts` in the
    JSON report -- only the confidence-annotated fields may differ. Spanning
    both ends, not just one arbitrary stub value, matters: a threshold-based
    gating bug (e.g. "downgrade status if confidence < 0.7") would otherwise
    survive undetected whenever the stub and the real value both happened to
    land on the same side of the threshold -- caught live during this task's
    own mutation testing, where a single fixed low-value stub (0.01) missed
    exactly such a mutant because the real value (0.60) was also low."""

    def test_status_and_counts_are_unaffected_by_the_identity_confidence_diagnostic(
        self, monkeypatch, tmp_path
    ):
        referrer = Entity.objects.create(entity_type="person", name="Agnew, Theodore, Lord")
        twin = Entity.objects.create(entity_type="person", name="AGNEW, Theodore Thomas More")
        supplier = Entity.objects.create(entity_type="company", name="TEST SUPPLIER LTD")
        same_as_edge = Edge.objects.create(
            edge_type="same_as", source_entity=referrer, target_entity=twin
        )
        Attestation.objects.create(
            edge=same_as_edge,
            source_name="Cross-register identity resolution",
            match_confidence=0.60,
        )
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=twin,
            target_entity=supplier,
            valid_from=date(2010, 1, 1),
        )

        cohort_csv = tmp_path / "cohort.csv"
        cohort_csv.write_text(
            "supplier_name,source_of_referral,actual_referrer,company_number\n"
            "TEST SUPPLIER LTD,Lord Agnew of Oulton,,\n",
            encoding="utf-8",
        )
        ch_cache = tmp_path / "ch_cache.json"
        ch_cache.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(phase_c_paths, "COHORT_CSV", str(cohort_csv))
        monkeypatch.setattr(phase_c_paths, "VIP_CH_CACHE", str(ch_cache))

        def run(confidence_fn, out_path):
            monkeypatch.setattr(phase_c_paths, "path_min_identity_confidence", confidence_fn)
            monkeypatch.setattr(sys, "argv", ["phase_c_paths.py", "--out", str(out_path)])
            phase_c_paths.main()
            return json.loads(out_path.read_text(encoding="utf-8"))

        real_report = run(phase_c_paths.path_min_identity_confidence, tmp_path / "real.json")
        low_report = run(lambda path: 0.0, tmp_path / "low.json")
        high_report = run(lambda path: 1.0, tmp_path / "high.json")

        for label, report in (("low", low_report), ("high", high_report)):
            assert report["counts"] == real_report["counts"], label
            report_statuses = [row["status"] for row in report["rows"]]
            real_statuses = [row["status"] for row in real_report["rows"]]
            assert report_statuses == real_statuses, label

        # Sanity: the diagnostic itself DID differ between the low and high
        # runs -- otherwise the assertions above would pass vacuously,
        # without the confidence value ever having mattered to anything.
        low_confidences = low_report["rows"][0]["pre_award_paths_min_identity_confidence"]
        high_confidences = high_report["rows"][0]["pre_award_paths_min_identity_confidence"]
        assert low_confidences != high_confidences
