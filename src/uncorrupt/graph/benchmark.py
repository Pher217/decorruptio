"""Relationship-recovery benchmark scorer.

Implements the acceptance criteria from ADR-005 D3 / spec v0.3 §7-bis:

1. **Laundering collapse**: corroboration — not edge inclusion — differs
   between modes. Every edge with at least one attestation is scoreable in
   both modes. Without collapse, an edge is "corroborated" once it has >=2
   attestations (naive count). With collapse, it is corroborated only once
   it has >=2 *independent* origins (following ``derived_from`` to the
   root). ``laundered_count`` reports edges with >=2 attestations but only
   1 independent origin — apparent corroboration that is really one source
   wearing several hats.
2. **Evidence-source-level holdout**: drop a source, and every edge attested
   *only* by that source drops out.
3. **Dual reporting**: corroboration reported both with and without
   laundering collapse so the delta is visible.

Precision is scoped to the *tested subgraph*: a false positive is an edge
between a golden-set entity pair that does not match any golden relationship
by edge_type. Edges between entity pairs the golden set never references are
excluded entirely (``untested_edges``) rather than counted as false
positives — precision answers "of the claims we make about tested entity
pairs, how many are correct", not "of everything in the graph".

Matching a golden relationship to an edge is entity + edge_type only
(non-temporal) — see ``_match_golden_to_edge``. Entities are matched by
registry identifier when the golden entry supplies one (ADR-004 D2),
falling back to normalised-name comparison otherwise; ``name_only_matches``
tracks how many true positives relied on the weaker, name-based path.

Additional metrics:
- Temporal accuracy: fraction of *matched* edges whose valid_from/valid_to
  fall within tolerance of the golden relationship's dates. A NULL edge
  date where the golden has a date is a miss, not a pass
  (``null_date_matches``).
- Resolution rate: fraction of golden relationships where both endpoints
  were resolved to a named entity (not an unresolved placeholder).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from django.db.models import QuerySet

from uncorrupt.graph.models import Attestation, Edge, Entity
from uncorrupt.staging.companies_house import _normalise_name


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
        source_registry_scheme: Registry scheme for the source entity, if known.
        source_registry_id: Registry ID for the source entity, if known. When
            supplied, matching prefers this over the source_name string.
        target_registry_scheme: Registry scheme for the target entity, if known.
        target_registry_id: Registry ID for the target entity, if known.
    """

    source_name: str
    target_name: str
    edge_type: str
    valid_from: date | None = None
    valid_to: date | None = None
    amount_cents: int | None = None
    source_registry_scheme: str | None = None
    source_registry_id: str | None = None
    target_registry_scheme: str | None = None
    target_registry_id: str | None = None


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
    corroborated_count: int
    laundered_count: int
    untested_edges: int
    null_date_matches: int
    name_only_matches: int

    def __str__(self) -> str:
        return (
            f"precision={self.precision:.3f} recall={self.recall:.3f} "
            f"tp={self.true_positives} fp={self.false_positives} "
            f"fn={self.false_negatives} untested={self.untested_edges} "
            f"temporal_acc={self.temporal_accuracy:.3f} "
            f"null_dates={self.null_date_matches} "
            f"resolution={self.resolution_rate:.3f} "
            f"corroborated={self.corroborated_count} laundered={self.laundered_count} "
            f"name_only_matches={self.name_only_matches} "
            f"(golden={self.total_golden} recovered={self.total_recovered})"
        )


@dataclass
class FullBenchmarkReport:
    """Full benchmark report with and without laundering collapse."""

    without_collapse: BenchmarkResult
    with_collapse: BenchmarkResult
    laundering_delta: int = 0

    def __str__(self) -> str:
        return (
            f"=== Without laundering collapse ===\n"
            f"  {self.without_collapse}\n"
            f"=== With laundering collapse ===\n"
            f"  {self.with_collapse}\n"
            f"=== Corroboration delta (collapse - no collapse) ===\n"
            f"  {self.laundering_delta:+d}"
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


def _match_entity(
    entity: Entity,
    name: str,
    registry_scheme: str | None,
    registry_id: str | None,
) -> str | None:
    """Match an entity to a golden relationship endpoint.

    Prefers registry identifier when the golden entry supplies one
    (ADR-004 D2); falls back to normalised-name comparison otherwise.

    Returns the method used ("registry_id" or "name") so name-based matches
    are visibly a weaker claim, or None if neither matches.
    """
    if (
        registry_id
        and entity.registry_id == registry_id
        and entity.registry_scheme == registry_scheme
    ):
        return "registry_id"
    if _normalise_name(entity.name) == _normalise_name(name):
        return "name"
    return None


def _match_golden_to_edge(golden: GoldenRelationship, edge: Edge) -> str | None:
    """Check if a graph edge matches a golden relationship's entities + edge_type.

    Matching is deliberately non-temporal — date agreement is measured
    separately (``_temporal_match`` / ``temporal_accuracy``) so it is a real
    quality metric rather than a silent recall gate.

    Returns the weakest match method used across both endpoints
    ("registry_id" if both endpoints matched by identifier, "name" if either
    fell back to normalised-name comparison), or None if no match.
    """
    if edge.edge_type != golden.edge_type:
        return None

    source_method = _match_entity(
        edge.source_entity,
        golden.source_name,
        golden.source_registry_scheme,
        golden.source_registry_id,
    )
    if source_method is None:
        return None

    target_method = _match_entity(
        edge.target_entity,
        golden.target_name,
        golden.target_registry_scheme,
        golden.target_registry_id,
    )
    if target_method is None:
        return None

    return "name" if "name" in (source_method, target_method) else "registry_id"


def _is_tested_pair(golden: list[GoldenRelationship], edge: Edge) -> bool:
    """True if edge's entities correspond to some golden relationship's entity
    pair, regardless of edge_type — i.e. this pair was in scope for testing.
    """
    for g in golden:
        source_method = _match_entity(
            edge.source_entity, g.source_name, g.source_registry_scheme, g.source_registry_id
        )
        if source_method is None:
            continue
        target_method = _match_entity(
            edge.target_entity, g.target_name, g.target_registry_scheme, g.target_registry_id
        )
        if target_method is not None:
            return True
    return False


def _is_resolved(entity: Entity) -> bool:
    """Check if an entity is resolved (not an unresolved placeholder).

    Unresolved placeholders carry a ``registry_scheme`` ending in
    "-UNRESOLVED" (e.g. GB-COH-OFFICER-UNRESOLVED, UK-PARLIAMENT-UNRESOLVED,
    UK-LORDS-UNRESOLVED), with ``registry_id`` of the form
    "{scope_id}:{normalised_name}".
    """
    if not entity.registry_scheme:
        return True
    return not entity.registry_scheme.endswith("-UNRESOLVED")


def _temporal_match(golden: GoldenRelationship, edge: Edge, tolerance: timedelta) -> bool:
    """Check if edge dates match golden dates within tolerance.

    A NULL edge date where the golden has a date is a temporal MISS, not a
    pass — see ``null_date_matches`` on ``BenchmarkResult``.
    """
    if golden.valid_from is not None:
        if edge.valid_from is None:
            return False
        if abs((edge.valid_from - golden.valid_from).days) > tolerance.days:
            return False
    if golden.valid_to is not None:
        if edge.valid_to is None:
            return False
        if abs((edge.valid_to - golden.valid_to).days) > tolerance.days:
            return False
    return True


def _is_null_date_miss(golden: GoldenRelationship, edge: Edge) -> bool:
    """True if the golden has a date the matched edge lacks (NULL)."""
    return (golden.valid_from is not None and edge.valid_from is None) or (
        golden.valid_to is not None and edge.valid_to is None
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

    # Score without laundering collapse (naive attestation-count corroboration)
    result_without = _score(
        golden_relationships,
        active_edges,
        temporal_tolerance,
        collapse_laundering=False,
    )

    # Score with laundering collapse (independent-origin corroboration)
    result_with = _score(
        golden_relationships,
        active_edges,
        temporal_tolerance,
        collapse_laundering=True,
    )

    delta = result_with.corroborated_count - result_without.corroborated_count

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

    Edge inclusion is identical in both modes: any edge with at least one
    attestation is scoreable. Laundering collapse changes only
    *corroboration* — see ``corroborated_count`` / ``laundered_count`` —
    never which edges are matched against golden relationships.

    Precision is scoped to the tested subgraph: false positives are edges
    between a golden-set entity pair that don't match any golden
    relationship by edge_type. Edges between untested entity pairs are
    excluded entirely (``untested_edges``).
    """
    substantiated_edges = [edge for edge in edges if len(edge.attestations.all()) > 0]

    corroborated_count = 0
    laundered_count = 0
    for edge in substantiated_edges:
        att_count = len(edge.attestations.all())
        origins = _get_independent_origins(edge)
        if collapse_laundering:
            if len(origins) >= 2:
                corroborated_count += 1
        elif att_count >= 2:
            corroborated_count += 1
        if att_count >= 2 and len(origins) == 1:
            laundered_count += 1

    # Match golden relationships to edges
    true_positives = 0
    matched_edge_ids: set[int] = set()
    temporal_matches = 0
    null_date_matches = 0
    resolved_count = 0
    name_only_matches = 0

    for g in golden:
        for edge in substantiated_edges:
            if edge.pk in matched_edge_ids:
                continue
            method = _match_golden_to_edge(g, edge)
            if method is None:
                continue
            matched_edge_ids.add(edge.pk)
            true_positives += 1
            if method == "name":
                name_only_matches += 1
            if _temporal_match(g, edge, tolerance):
                temporal_matches += 1
            elif _is_null_date_miss(g, edge):
                null_date_matches += 1
            if _is_resolved(edge.source_entity) and _is_resolved(edge.target_entity):
                resolved_count += 1
            break

    false_positives = 0
    untested_edges = 0
    for edge in substantiated_edges:
        if edge.pk in matched_edge_ids:
            continue
        if _is_tested_pair(golden, edge):
            false_positives += 1
        else:
            untested_edges += 1

    false_negatives = len(golden) - true_positives

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
        false_positives=false_positives,
        false_negatives=false_negatives,
        temporal_accuracy=temporal_accuracy,
        resolution_rate=resolution_rate,
        total_golden=len(golden),
        total_recovered=len(substantiated_edges),
        corroborated_count=corroborated_count,
        laundered_count=laundered_count,
        untested_edges=untested_edges,
        null_date_matches=null_date_matches,
        name_only_matches=name_only_matches,
    )
