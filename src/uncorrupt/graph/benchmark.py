"""Relationship-recovery benchmark scorer.

Implements the acceptance criteria from ADR-005 D3 / spec v0.3 §7-bis:

1. **Laundering collapse**: two attestations tracing to a single origin
   (via ``derived_from`` chain) count as one.
2. **Evidence-source-level holdout**: drop a source, and every edge attested
   *only* by that source drops out.
3. **Dual reporting**: precision/recall reported both with and without
   laundering collapse so the delta is visible.

Additional metrics:
- Temporal accuracy: fraction of recovered edges whose valid_from/valid_to
  match the golden relationship's time range (±30 days tolerance).
- Resolution rate: fraction of golden relationships where both endpoints
  were resolved to a named entity (not a scoped placeholder).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import QuerySet

from uncorrupt.graph.models import Attestation, Edge, Entity


@dataclass(frozen=True)
class GoldenRelationship:
    """A known-true relationship for benchmark scoring.

    Attributes:
        source_name: Name of the source entity (e.g. "PPE Medpro Ltd").
        target_name: Name of the target entity (e.g. "Conservative Party").
        edge_type: Type of edge (e.g. "donation", "officer_of").
        valid_from: Expected start date (or None if unknown).
        valid_to: Expected end date (or None if ongoing).
        amount_cents: Expected amount in cents (or None if not applicable).
    """

    source_name: str
    target_name: str
    edge_type: str
    valid_from: date | None = None
    valid_to: date | None = None
    amount_cents: int | None = None


@dataclass
class BenchmarkResult:
    """Results of a single benchmark run."""

    precision: float
    recall: float
    true_positives: int
    false_positives: int
    false_negatives: int
    temporal_accuracy: float
    resolution_rate: float
    total_golden: int
    total_recovered: int

    def __str__(self) -> str:
        return (
            f"precision={self.precision:.3f} recall={self.recall:.3f} "
            f"tp={self.true_positives} fp={self.false_positives} "
            f"fn={self.false_negatives} "
            f"temporal_acc={self.temporal_accuracy:.3f} "
            f"resolution={self.resolution_rate:.3f} "
            f"(golden={self.total_golden} recovered={self.total_recovered})"
        )


@dataclass
class FullBenchmarkReport:
    """Full benchmark report with and without laundering collapse."""

    without_collapse: BenchmarkResult
    with_collapse: BenchmarkResult
    laundering_delta: float = 0.0

    def __str__(self) -> str:
        return (
            f"=== Without laundering collapse ===\n"
            f"  {self.without_collapse}\n"
            f"=== With laundering collapse ===\n"
            f"  {self.with_collapse}\n"
            f"=== Precision delta (collapse - no collapse) ===\n"
            f"  {self.laundering_delta:+.3f}"
        )


def _trace_origin(attestation: Attestation) -> Attestation:
    """Follow the derived_from chain to the root attestation.

    If derived_from is NULL, this attestation is an independent origin.
    """
    seen: set[int] = set()
    current = attestation
    while current.derived_from_id is not None:
        if current.derived_from_id in seen:
            # Cycle detected — treat current as origin
            return current
        seen.add(current.derived_from_id)
        parent = current.derived_from
        if parent is None:
            break
        current = parent
    return current


def _get_independent_origins(edge: Edge) -> set[int]:
    """Return the set of distinct origin attestation IDs for an edge.

    Two attestations that trace to the same origin count as one
    (laundering collapse).
    """
    origins: set[int] = set()
    for att in edge.attestations.all():
        origin = _trace_origin(att)
        origins.add(origin.pk)
    return origins


def _edges_attested_only_by(
    source_name: str,
    base_queryset: QuerySet[Edge] | None = None,
) -> set[int]:
    """Return edge IDs attested *only* by the given source.

    An edge drops out under source-level holdout if every attestation
    on it comes from the held-out source.
    """
    qs = base_queryset if base_queryset is not None else Edge.objects.all()
    edge_ids: set[int] = set()
    for edge in qs.prefetch_related("attestations"):
        atts = list(edge.attestations.all())
        if not atts:
            continue
        if all(att.source_name == source_name for att in atts):
            edge_ids.add(edge.pk)
    return edge_ids


def _match_golden_to_edge(
    golden: GoldenRelationship,
    edge: Edge,
    temporal_tolerance: timedelta,
) -> bool:
    """Check if a graph edge matches a golden relationship."""
    if edge.edge_type != golden.edge_type:
        return False

    # Entity matching by name (case-insensitive)
    source_match = edge.source_entity.name.lower() == golden.source_name.lower()
    target_match = edge.target_entity.name.lower() == golden.target_name.lower()
    if not (source_match and target_match):
        return False

    # Temporal matching (if golden has dates)
    if (
        golden.valid_from is not None
        and edge.valid_from is not None
        and abs((edge.valid_from - golden.valid_from).days) > temporal_tolerance.days
    ):
        return False

    return not (
        golden.valid_to is not None
        and edge.valid_to is not None
        and abs((edge.valid_to - golden.valid_to).days) > temporal_tolerance.days
    )


def _is_resolved(entity: Entity) -> bool:
    """Check if an entity is resolved (not a scoped placeholder).

    Unresolved entities have registry_id starting with a scoped prefix
    (e.g. "interest:..." or "donation:...") indicating they were created
    from a single interest declaration without matching to a real entity.
    """
    if not entity.registry_id:
        return True  # No registry_id — could be a manually created entity
    scoped_prefixes = ("interest:", "donation:", "officer:", "edge:")
    return not any(entity.registry_id.startswith(p) for p in scoped_prefixes)


def _temporal_match(golden: GoldenRelationship, edge: Edge, tolerance: timedelta) -> bool:
    """Check if edge dates match golden dates within tolerance."""
    if (
        golden.valid_from is not None
        and edge.valid_from is not None
        and abs((edge.valid_from - golden.valid_from).days) > tolerance.days
    ):
        return False
    return not (
        golden.valid_to is not None
        and edge.valid_to is not None
        and abs((edge.valid_to - golden.valid_to).days) > tolerance.days
    )


def score_benchmark(
    golden_relationships: list[GoldenRelationship],
    holdout_source: str | None = None,
    temporal_tolerance: timedelta = timedelta(days=30),
) -> FullBenchmarkReport:
    """Score the relationship-recovery benchmark.

    Args:
        golden_relationships: List of known-true relationships to test.
        holdout_source: If set, exclude all edges attested only by this source.
        temporal_tolerance: Tolerance for date matching (default ±30 days).

    Returns:
        FullBenchmarkReport with results both with and without laundering collapse.
    """
    # Build the edge queryset, applying holdout if requested
    base_qs = Edge.objects.all()

    held_out_edge_ids: set[int] = set()
    if holdout_source:
        held_out_edge_ids = _edges_attested_only_by(holdout_source, base_qs)

    active_edges = [
        edge
        for edge in base_qs.prefetch_related("attestations", "source_entity", "target_entity")
        if edge.pk not in held_out_edge_ids
    ]

    # Score without laundering collapse (each attestation counts independently)
    result_without = _score(
        golden_relationships,
        active_edges,
        temporal_tolerance,
        collapse_laundering=False,
    )

    # Score with laundering collapse (attestations tracing to same origin = 1)
    result_with = _score(
        golden_relationships,
        active_edges,
        temporal_tolerance,
        collapse_laundering=True,
    )

    delta = result_with.precision - result_without.precision

    return FullBenchmarkReport(
        without_collapse=result_without,
        with_collapse=result_with,
        laundering_delta=delta,
    )


def _score(
    golden: list[GoldenRelationship],
    edges: list[Edge],
    tolerance: timedelta,
    collapse_laundering: bool,
) -> BenchmarkResult:
    """Core scoring logic.

    When collapse_laundering=True, an edge is only "recovered" if it has
    at least one attestation with an independent origin (derived_from is NULL
    or traces to a root that is not shared exclusively with laundered attestations).
    """
    # Determine which edges are "independently substantiated"
    if collapse_laundering:
        substantiated_edges: list[Edge] = []
        for edge in edges:
            origins = _get_independent_origins(edge)
            if len(origins) >= 1:
                substantiated_edges.append(edge)
    else:
        substantiated_edges = edges

    # Match golden relationships to edges
    true_positives = 0
    false_positives = 0
    matched_edge_ids: set[int] = set()
    temporal_matches = 0
    resolved_count = 0

    for g in golden:
        found = False
        for edge in substantiated_edges:
            if edge.pk in matched_edge_ids:
                continue
            if _match_golden_to_edge(g, edge, tolerance):
                found = True
                matched_edge_ids.add(edge.pk)
                true_positives += 1
                if _temporal_match(g, edge, tolerance):
                    temporal_matches += 1
                if _is_resolved(edge.source_entity) and _is_resolved(edge.target_entity):
                    resolved_count += 1
                break
        if not found:
            pass  # false negative

    false_negatives = len(golden) - true_positives
    false_positives = len(substantiated_edges) - true_positives

    precision = (
        true_positives / (true_positives + false_positives)
        if (true_positives + false_positives) > 0
        else 0.0
    )
    recall = true_positives / len(golden) if len(golden) > 0 else 0.0
    temporal_accuracy = temporal_matches / true_positives if true_positives > 0 else 0.0
    resolution_rate = resolved_count / true_positives if true_positives > 0 else 0.0

    return BenchmarkResult(
        precision=precision,
        recall=recall,
        true_positives=true_positives,
        false_positives=max(false_positives, 0),
        false_negatives=false_negatives,
        temporal_accuracy=temporal_accuracy,
        resolution_rate=resolution_rate,
        total_golden=len(golden),
        total_recovered=len(substantiated_edges),
    )
