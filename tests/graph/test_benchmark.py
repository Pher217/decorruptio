"""Tests for the relationship-recovery benchmark scorer.

Tests the acceptance criteria from ADR-005 D3 / spec v0.3 §7-bis:
1. Two attestations tracing to one origin count as one (laundering collapse).
2. Holdout excludes a source; all edges attested only by that source drop out.
3. Precision/recall reported both with and without laundering collapse.
"""

from datetime import UTC, datetime, timedelta

import pytest

from uncorrupt.graph.benchmark import (
    GoldenRelationship,
    _edges_attested_only_by,
    _get_independent_origins,
    _is_resolved,
    _trace_origin,
    score_benchmark,
)
from uncorrupt.graph.models import Attestation, Edge, Entity


@pytest.mark.django_db
class TestLaunderingCollapse:
    """Acceptance criterion: two attestations tracing to one origin count as one."""

    def test_two_attestations_same_origin_count_as_one(self):
        """Two attestations where one derives_from the other → 1 origin."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
            amount_cents=50000,
        )
        att1 = Attestation.objects.create(
            edge=edge,
            source_name="Source A",
            source_reference="ref-a-1",
        )
        Attestation.objects.create(
            edge=edge,
            source_name="Source B",
            source_reference="ref-b-1",
            derived_from=att1,  # derives from att1 → same origin
        )

        origins = _get_independent_origins(edge)
        assert len(origins) == 1  # Only 1 independent origin

    def test_two_independent_attestations_count_as_two(self):
        """Two attestations with no derived_from → 2 independent origins."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
        )
        Attestation.objects.create(edge=edge, source_name="Source A", source_reference="ref-a")
        Attestation.objects.create(edge=edge, source_name="Source B", source_reference="ref-b")

        origins = _get_independent_origins(edge)
        assert len(origins) == 2

    def test_chain_traces_to_root(self):
        """A→B→C chain traces back to A as the root origin."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
        )
        att1 = Attestation.objects.create(
            edge=edge, source_name="Source A", source_reference="ref-a"
        )
        att2 = Attestation.objects.create(
            edge=edge,
            source_name="Source B",
            source_reference="ref-b",
            derived_from=att1,
        )
        att3 = Attestation.objects.create(
            edge=edge,
            source_name="Source C",
            source_reference="ref-c",
            derived_from=att2,
        )

        root = _trace_origin(att3)
        assert root.pk == att1.pk

        origins = _get_independent_origins(edge)
        assert len(origins) == 1


@pytest.mark.django_db
class TestSourceHoldout:
    """Acceptance criterion: holdout excludes a source → edges attested only by it drop out."""

    def test_edge_attested_only_by_held_out_source_drops(self):
        """An edge with only one attestation from the held-out source drops."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
        )
        Attestation.objects.create(
            edge=edge, source_name="Electoral Commission", source_reference="ref-1"
        )

        dropped = _edges_attested_only_by("Electoral Commission")
        assert edge.pk in dropped

    def test_edge_with_multiple_sources_survives_holdout(self):
        """An edge with attestations from multiple sources survives."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
        )
        Attestation.objects.create(
            edge=edge, source_name="Electoral Commission", source_reference="ref-1"
        )
        Attestation.objects.create(
            edge=edge, source_name="Parliament Register", source_reference="ref-2"
        )

        dropped = _edges_attested_only_by("Electoral Commission")
        assert edge.pk not in dropped

    def test_holdout_in_score_benchmark_drops_exclusive_edges(self):
        """score_benchmark with holdout_source removes exclusively-attested edges."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
        )
        Attestation.objects.create(
            edge=edge, source_name="Electoral Commission", source_reference="ref-1"
        )

        golden = [
            GoldenRelationship(
                source_name="Corp Ltd",
                target_name="Party A",
                edge_type="donation",
            )
        ]

        # Without holdout → should find the edge
        report = score_benchmark(golden)
        assert report.without_collapse.recall == 1.0

        # With holdout → edge drops, recall = 0
        report_held = score_benchmark(golden, holdout_source="Electoral Commission")
        assert report_held.without_collapse.recall == 0.0


@pytest.mark.django_db
class TestDualReporting:
    """Acceptance criterion: corroboration reported with and without laundering
    collapse; edge inclusion stays identical between modes."""

    def test_laundering_delta_reflects_corroboration_difference(self):
        """laundering_delta is the difference in corroborated_count between
        modes, not a precision delta — edge inclusion never changes."""
        # Edge 1: genuinely corroborated — 2 independent origins
        s1 = Entity.objects.create(entity_type="company", name="Real Corp Ltd")
        t1 = Entity.objects.create(entity_type="political_party", name="Party A")
        edge1 = Edge.objects.create(edge_type="donation", source_entity=s1, target_entity=t1)
        Attestation.objects.create(edge=edge1, source_name="EC", source_reference="r1")
        Attestation.objects.create(edge=edge1, source_name="Parliament", source_reference="r2")

        # Edge 2: laundered — 2 attestations, 1 independent origin
        s2 = Entity.objects.create(entity_type="company", name="Suspicious Ltd")
        t2 = Entity.objects.create(entity_type="political_party", name="Party B")
        edge2 = Edge.objects.create(edge_type="donation", source_entity=s2, target_entity=t2)
        att_a = Attestation.objects.create(
            edge=edge2, source_name="Source A", source_reference="r3"
        )
        Attestation.objects.create(
            edge=edge2,
            source_name="Source B",
            source_reference="r4",
            derived_from=att_a,
        )

        golden = [
            GoldenRelationship(
                source_name="Real Corp Ltd",
                target_name="Party A",
                edge_type="donation",
            )
        ]

        report = score_benchmark(golden)

        # Edge inclusion is identical in both modes.
        assert report.without_collapse.total_recovered == 2
        assert report.with_collapse.total_recovered == 2

        # Corroboration differs: edge2 is naive-corroborated (2 attestations)
        # but not independent-origin-corroborated (1 origin).
        assert report.without_collapse.corroborated_count == 2
        assert report.with_collapse.corroborated_count == 1
        assert report.with_collapse.laundered_count == 1
        assert report.laundering_delta == -1


@pytest.mark.django_db
class TestCorroborationMetrics:
    """Acceptance criterion: laundering collapse changes corroboration, not
    edge inclusion (benchmark.py FIX 1)."""

    def test_single_origin_edge_not_corroborated_under_collapse(self):
        """3 attestations tracing to one origin: naive-corroborated (>=2
        attestations) but NOT independent-origin-corroborated."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(edge_type="donation", source_entity=source, target_entity=target)
        att1 = Attestation.objects.create(edge=edge, source_name="Source A", source_reference="r1")
        Attestation.objects.create(
            edge=edge, source_name="Source B", source_reference="r2", derived_from=att1
        )
        Attestation.objects.create(
            edge=edge, source_name="Source C", source_reference="r3", derived_from=att1
        )

        report = score_benchmark([])

        assert report.without_collapse.corroborated_count == 1
        assert report.with_collapse.corroborated_count == 0

    def test_distinct_origin_edge_corroborated_in_both_modes(self):
        """3 attestations from 3 distinct origins: corroborated in both modes."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(edge_type="donation", source_entity=source, target_entity=target)
        Attestation.objects.create(edge=edge, source_name="Source A", source_reference="r1")
        Attestation.objects.create(edge=edge, source_name="Source B", source_reference="r2")
        Attestation.objects.create(edge=edge, source_name="Source C", source_reference="r3")

        report = score_benchmark([])

        assert report.without_collapse.corroborated_count == 1
        assert report.with_collapse.corroborated_count == 1

    def test_laundered_count_identifies_single_origin_multi_attestation(self):
        """laundered_count flags edges with >=2 attestations but only 1
        independent origin — apparent corroboration that is one source."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(edge_type="donation", source_entity=source, target_entity=target)
        att1 = Attestation.objects.create(edge=edge, source_name="Source A", source_reference="r1")
        Attestation.objects.create(
            edge=edge, source_name="Source B", source_reference="r2", derived_from=att1
        )

        report = score_benchmark([])

        assert report.without_collapse.laundered_count == 1
        assert report.with_collapse.laundered_count == 1


@pytest.mark.django_db
class TestPrecisionScoping:
    """Acceptance criterion: precision is scoped to the tested subgraph
    (benchmark.py FIX 2)."""

    def test_precision_unaffected_by_untested_pairs(self):
        """Edges between entity pairs outside the golden set don't count as
        false positives — they're excluded as untested_edges."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(edge_type="donation", source_entity=source, target_entity=target)
        Attestation.objects.create(edge=edge, source_name="EC", source_reference="r1")

        golden = [
            GoldenRelationship(source_name="Corp Ltd", target_name="Party A", edge_type="donation")
        ]

        for i in range(20):
            other_source = Entity.objects.create(entity_type="company", name=f"Other Co {i}")
            other_target = Entity.objects.create(
                entity_type="political_party", name=f"Other Party {i}"
            )
            other_edge = Edge.objects.create(
                edge_type="donation", source_entity=other_source, target_entity=other_target
            )
            Attestation.objects.create(
                edge=other_edge, source_name="EC", source_reference=f"r-other-{i}"
            )

        report = score_benchmark(golden)

        assert report.without_collapse.true_positives == 1
        assert report.without_collapse.false_positives == 0
        assert report.without_collapse.untested_edges == 20
        assert report.without_collapse.precision == 1.0

    def test_wrong_edge_type_on_tested_pair_is_false_positive(self):
        """An edge between a golden-set entity pair with the wrong edge_type
        is a false positive, not an untested edge."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(
            edge_type="officer_of", source_entity=source, target_entity=target
        )
        Attestation.objects.create(edge=edge, source_name="EC", source_reference="r1")

        golden = [
            GoldenRelationship(source_name="Corp Ltd", target_name="Party A", edge_type="donation")
        ]

        report = score_benchmark(golden)

        assert report.without_collapse.true_positives == 0
        assert report.without_collapse.false_positives == 1
        assert report.without_collapse.untested_edges == 0


@pytest.mark.django_db
class TestRegistryIdMatching:
    """Acceptance criterion: match by registry identifier when available,
    falling back to normalised-name comparison (benchmark.py FIX 5)."""

    def test_registry_id_matches_where_name_would_fail(self):
        """Registry-ID matching succeeds for entities whose names differ in
        formatting ("PPE Medpro Ltd" vs "PPE MEDPRO LIMITED")."""
        source = Entity.objects.create(
            entity_type="company",
            name="PPE MEDPRO LIMITED",
            registry_scheme="GB-COH",
            registry_id="12345678",
        )
        target = Entity.objects.create(
            entity_type="political_party",
            name="Party A",
            registry_scheme="UK-PARTY",
            registry_id="PP1",
        )
        edge = Edge.objects.create(edge_type="donation", source_entity=source, target_entity=target)
        Attestation.objects.create(edge=edge, source_name="EC", source_reference="r1")

        golden = [
            GoldenRelationship(
                source_name="PPE Medpro Ltd",
                target_name="Party A",
                edge_type="donation",
                source_registry_scheme="GB-COH",
                source_registry_id="12345678",
                target_registry_scheme="UK-PARTY",
                target_registry_id="PP1",
            )
        ]

        report = score_benchmark(golden)

        assert report.without_collapse.true_positives == 1
        assert report.without_collapse.name_only_matches == 0

    def test_name_fallback_when_no_registry_id_supplied(self):
        """Without a registry identifier on the golden entry, matching falls
        back to normalised-name comparison and is recorded as such."""
        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(edge_type="donation", source_entity=source, target_entity=target)
        Attestation.objects.create(edge=edge, source_name="EC", source_reference="r1")

        golden = [
            GoldenRelationship(source_name="Corp Ltd", target_name="Party A", edge_type="donation")
        ]

        report = score_benchmark(golden)

        assert report.without_collapse.true_positives == 1
        assert report.without_collapse.name_only_matches == 1


@pytest.mark.django_db
class TestTemporalAccuracy:
    """Test temporal accuracy metric."""

    def test_temporal_match_within_tolerance(self):
        """Edge dates within tolerance of golden → temporal match."""
        from datetime import date

        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
            valid_from=date(2020, 1, 20),  # 15 days off from golden
        )
        Attestation.objects.create(
            edge=Edge.objects.get(),
            source_name="EC",
            source_reference="r1",
            observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

        golden = [
            GoldenRelationship(
                source_name="Corp Ltd",
                target_name="Party A",
                edge_type="donation",
                valid_from=date(2020, 1, 5),
            )
        ]

        report = score_benchmark(golden, temporal_tolerance=timedelta(days=30))
        assert report.without_collapse.temporal_accuracy == 1.0

    def test_temporal_mismatch_outside_tolerance(self):
        """Edge dates outside tolerance → still a match (matching is
        non-temporal), but temporal_accuracy drops below 1.0."""
        from datetime import date

        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
            valid_from=date(2020, 6, 1),  # ~5 months off
        )
        Attestation.objects.create(edge=edge, source_name="EC", source_reference="r1")

        golden = [
            GoldenRelationship(
                source_name="Corp Ltd",
                target_name="Party A",
                edge_type="donation",
                valid_from=date(2020, 1, 5),
            )
        ]

        report = score_benchmark(golden, temporal_tolerance=timedelta(days=30))
        assert report.without_collapse.true_positives == 1
        assert report.without_collapse.recall == 1.0
        assert report.without_collapse.temporal_accuracy == 0.0

    def test_null_valid_from_counts_as_temporal_miss(self):
        """Edge with NULL valid_from against a golden with a date is a
        temporal miss, not a pass — tracked via null_date_matches."""
        from datetime import date

        source = Entity.objects.create(entity_type="company", name="Corp Ltd")
        target = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(
            edge_type="donation",
            source_entity=source,
            target_entity=target,
        )
        Attestation.objects.create(edge=edge, source_name="EC", source_reference="r1")

        golden = [
            GoldenRelationship(
                source_name="Corp Ltd",
                target_name="Party A",
                edge_type="donation",
                valid_from=date(2020, 1, 5),
            )
        ]

        report = score_benchmark(golden)
        assert report.without_collapse.true_positives == 1
        assert report.without_collapse.temporal_accuracy == 0.0
        assert report.without_collapse.null_date_matches == 1


@pytest.mark.django_db
class TestResolutionRate:
    """Test resolution rate metric."""

    def test_resolved_entities_count(self):
        """Both entities resolved → resolution_rate = 1.0."""
        source = Entity.objects.create(
            entity_type="company",
            name="Corp Ltd",
            registry_scheme="GB-COH",
            registry_id="12345678",
        )
        target = Entity.objects.create(
            entity_type="political_party",
            name="Party A",
            registry_scheme="UK-PARTY",
            registry_id="PP1",
        )
        edge = Edge.objects.create(edge_type="donation", source_entity=source, target_entity=target)
        Attestation.objects.create(edge=edge, source_name="EC", source_reference="r1")

        golden = [
            GoldenRelationship(
                source_name="Corp Ltd",
                target_name="Party A",
                edge_type="donation",
            )
        ]

        report = score_benchmark(golden)
        assert report.without_collapse.resolution_rate == 1.0

    def test_unresolved_entity_lowers_resolution_rate(self):
        """One unresolved entity → resolution_rate = 0.0 for that match."""
        source = Entity.objects.create(
            entity_type="company",
            name="Corp Ltd",
            registry_scheme="GB-COH",
            registry_id="12345678",
        )
        # Unresolved: registry_scheme ends in -UNRESOLVED
        target = Entity.objects.create(
            entity_type="political_party",
            name="Party A",
            registry_scheme="UK-PARLIAMENT-UNRESOLVED",
            registry_id="12345:party a",
        )
        edge = Edge.objects.create(edge_type="donation", source_entity=source, target_entity=target)
        Attestation.objects.create(edge=edge, source_name="EC", source_reference="r1")

        golden = [
            GoldenRelationship(
                source_name="Corp Ltd",
                target_name="Party A",
                edge_type="donation",
            )
        ]

        report = score_benchmark(golden)
        assert report.without_collapse.resolution_rate == 0.0

    def test_is_resolved_scoped_entity(self):
        """Entities whose registry_scheme ends in -UNRESOLVED are unresolved."""
        scoped = Entity.objects.create(
            entity_type="company",
            name="Unknown Corp",
            registry_scheme="GB-COH-OFFICER-UNRESOLVED",
            registry_id="12345678:unknown corp",
        )
        assert not _is_resolved(scoped)

    def test_is_resolved_real_entity(self):
        """Entities with real registry_id are resolved."""
        real = Entity.objects.create(
            entity_type="company",
            name="Real Corp",
            registry_scheme="GB-COH",
            registry_id="12345678",
        )
        assert _is_resolved(real)


@pytest.mark.django_db
class TestEmptyBenchmark:
    """Test edge cases with empty data."""

    def test_no_golden_no_edges(self):
        """Empty golden and empty graph → all zeros."""
        report = score_benchmark([])
        assert report.without_collapse.precision == 0.0
        assert report.without_collapse.recall == 0.0
        assert report.without_collapse.true_positives == 0

    def test_golden_but_no_edges(self):
        """Golden relationships but no edges → recall = 0, FN = len(golden)."""
        golden = [
            GoldenRelationship(
                source_name="Corp Ltd",
                target_name="Party A",
                edge_type="donation",
            )
        ]
        report = score_benchmark(golden)
        assert report.without_collapse.recall == 0.0
        assert report.without_collapse.false_negatives == 1
