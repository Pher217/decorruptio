"""Read-only tool functions exposed by the MCP server (`server.py`).

Plain Python functions with no MCP-framework dependency -- every one of them
is directly unit-testable (`tests/mcp/test_tools.py`) without spinning up the
protocol layer, and `server.py` registers them as-is via
`MCPServer.tool()(fn)`.

No tool here ever mutates. Every ORM call is a read (`.get`/`.filter`/
`.values*`/`.annotate`); none calls `.save`/`.create`/`.update`/`.delete`/
`.bulk_create`/`.bulk_update`/`.get_or_create`/`.update_or_create` --
`tests/mcp/test_read_only.py` statically scans this module's source for
those call patterns as a second, independent guarantee beyond "we didn't
write one".

Path search delegates entirely to `scripts.phase_c_paths` (`build_adjacency`,
`find_paths`) rather than re-implementing traversal -- the project's
explicit anti-script-sprawl rule (see `scripts/run_gold_benchmark.py`'s
docstring: "This script does NOT implement a second path-search or
resolution stack.").
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from django.db.models import Count, Q
from scripts.phase_c_paths import build_adjacency
from scripts.phase_c_paths import find_paths as _phase_c_find_paths

from uncorrupt.graph import ch_officers
from uncorrupt.graph.models import Alias, Edge, Entity
from uncorrupt.graph.register_snapshots import path_min_identity_confidence
from uncorrupt.mcp.privacy import entity_summary
from uncorrupt.register.loader import all_sources

_README_PATH = Path(__file__).resolve().parents[3] / "sources" / "README.md"

# Server-side ceilings -- a caller-supplied bound is a request, not a grant.
# Against a ~288k-row Entity table, an unclamped `limit` or an unclamped
# `find_paths` hop count both turn a single tool call into a full-table or
# combinatorial scan. Both ceilings are enforced in the ORM query itself
# (`qs[:effective_limit]`), not by materializing everything and slicing
# after -- so the database itself never returns more than the cap.
_MAX_RESOLVE_LIMIT = 200
_MAX_FIND_PATHS_HOPS = 4


def resolve_entity(
    name: str | None = None,
    company_number: str | None = None,
    registry_scheme: str | None = None,
    registry_id: str | None = None,
    entity_type: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """Resolve-or-refuse entity lookup. Returns CANDIDATES, never a single guessed match.

    At least one of `name`, `company_number`, or `registry_id` is required;
    `registry_scheme` alone raises `ValueError` -- it narrows any of the
    other filters to one register (e.g. only `GB-COH`), which matters once
    more than one country's connectors are onboarded and a `registry_id`
    could otherwise collide across schemes, but a scheme by itself is not a
    lookup key: `Entity`'s own uniqueness constraint is (`registry_scheme`,
    `registry_id`) together, and silently returning "every GB-COH entity"
    through a resolution tool would dump ~200k rows with no anchor at all.
    `entity_type` narrows to one of `Entity.ENTITY_TYPES` (e.g. "company").

    Resolution is by registry identifier first wherever one is given
    (ADR-004 D2 -- never resolve by name string alone); a name match is a
    plain case-insensitive substring search over both `Entity.name` and any
    `Alias.name` (trading names, former names). Ties are always returned as
    separate candidates, never collapsed to one guess (the project's
    "duplicate over merge" principle, ADR-006) -- the caller disambiguates.

    `limit` is clamped server-side to `[1, _MAX_RESOLVE_LIMIT]` and pushed
    into each contributing queryset's own slice (`qs[:effective_limit]`) --
    never materialized in full and sliced afterwards. The clamp is
    silent-proof: the response's `limit` key is the EFFECTIVE value actually
    used, so a caller that asked for more can see it was capped rather than
    believing it got what it asked for.
    """
    if not any([name, company_number, registry_id]):
        raise ValueError(
            "resolve_entity requires at least one of: name, company_number, or registry_id"
        )
    effective_limit = max(1, min(limit, _MAX_RESOLVE_LIMIT))

    base = Entity.objects.all()
    if entity_type:
        base = base.filter(entity_type=entity_type)
    if registry_scheme:
        base = base.filter(registry_scheme=registry_scheme)

    matches: dict[int, Entity] = {}

    if registry_id:
        for e in base.filter(registry_id=registry_id)[:effective_limit]:
            matches[e.id] = e
    if company_number:
        for e in base.filter(company_number=company_number)[:effective_limit]:
            matches[e.id] = e
    if name:
        for e in base.filter(name__icontains=name)[:effective_limit]:
            matches[e.id] = e
        alias_entity_ids = list(
            Alias.objects.filter(name__icontains=name).values_list("entity_id", flat=True)[
                :effective_limit
            ]
        )
        if alias_entity_ids:
            for e in base.filter(id__in=alias_entity_ids)[:effective_limit]:
                matches[e.id] = e

    candidates = [entity_summary(e) for e in list(matches.values())[:effective_limit]]
    return {"limit": effective_limit, "candidates": candidates}


def get_entity(entity_id: int) -> dict[str, Any]:
    """The entity plus its edge counts by type (both directions combined).

    Raises `Entity.DoesNotExist` if `entity_id` does not resolve to a row --
    exploration should fail loudly on a bad id, not silently return nothing.
    """
    entity = Entity.objects.get(pk=entity_id)
    summary = entity_summary(entity)
    counts: dict[str, int] = defaultdict(int)
    rows = (
        Edge.objects.filter(Q(source_entity_id=entity_id) | Q(target_entity_id=entity_id))
        .values("edge_type")
        .annotate(n=Count("id"))
        .values_list("edge_type", "n")
    )
    for edge_type, n in rows:
        counts[edge_type] += n
    summary["edge_counts_by_type"] = dict(counts)
    return summary


def _serialize_edge(edge: Edge) -> dict[str, Any]:
    return {
        "edge_id": edge.id,
        "edge_type": edge.edge_type,
        "source_entity_id": edge.source_entity_id,
        "target_entity_id": edge.target_entity_id,
        "valid_from": edge.valid_from.isoformat() if edge.valid_from else None,
        "attesting_sources": sorted(set(edge.attestations.values_list("source_name", flat=True))),
    }


def find_paths(source_id: int, target_id: int, max_hops: int = 2) -> dict[str, Any]:
    """Paths of at most `max_hops` between two entities, direction ignored.

    Delegates entirely to `scripts.phase_c_paths.build_adjacency` +
    `find_paths` -- the same traversal Phase C's benchmark and controls use
    -- rather than a second implementation. Called with `cutoff=date.max`
    (every path found, not just ones dated before some award cutoff): the
    pre-award-cutoff framing is specific to the Phase C benchmark question,
    not to general graph exploration. Each edge on a path reports its type,
    `valid_from`, and the names of the sources that attest it (see
    `get_attestations` for the full evidence record behind any one edge).

    Each path also carries `min_identity_confidence`
    (`register_snapshots.path_min_identity_confidence`) -- the same
    STRICTLY POST-HOC, EXPLORATORY, NON-GATING diagnostic
    `scripts/phase_c_paths.py`'s own report attaches, surfaced here too
    because this tool is the layer closest to a published claim: without
    it, a path bridged by a 0.60 "surname + peerage title only" identity
    guess rendered identically to one bridged by a registry identifier, and
    `get_attestations` only exposes the underlying confidence on a second,
    separate call the caller would have to know to make. Reporting this
    value NEVER filters, reorders, or drops a path -- see that function's
    own docstring for why it is uncalibrated and must never be read as a
    probability. `None` means either no identity bridge on the path, or an
    unattested one (see that docstring); never confuse it with 1.0
    ("certain").

    `max_hops` is clamped server-side to `[1, _MAX_FIND_PATHS_HOPS]` before
    the walk runs -- the locked Phase C benchmark only ever needs 2, so even
    the ceiling is generous, and an unbounded hop count on a 400k+-edge graph
    is a denial-of-service against this project's own database. The clamp is
    silent-proof: the response's `max_hops` key is the EFFECTIVE value the
    walk actually used, not the raw input, so a caller that asked for more
    can see it was capped.

    Raises `Entity.DoesNotExist` if either id does not resolve to a row.
    """
    if not Entity.objects.filter(pk=source_id).exists():
        raise Entity.DoesNotExist(f"source entity {source_id} not found")
    if not Entity.objects.filter(pk=target_id).exists():
        raise Entity.DoesNotExist(f"target entity {target_id} not found")
    effective_max_hops = max(1, min(max_hops, _MAX_FIND_PATHS_HOPS))

    adjacency = build_adjacency()
    dated, undated = _phase_c_find_paths(
        {source_id}, target_id, adjacency, effective_max_hops, cutoff=date.max
    )
    return {
        "source_id": source_id,
        "target_id": target_id,
        "max_hops": effective_max_hops,
        "paths": [
            {
                "hops": len(path),
                "edges": [_serialize_edge(e) for e in path],
                "min_identity_confidence": path_min_identity_confidence(path),
            }
            for path in [*dated, *undated]
        ],
    }


def get_attestations(edge_id: int) -> dict[str, Any]:
    """Evidence for one claim: every `Attestation` recorded against an edge.

    Audited for an unbounded caller-supplied query alongside `resolve_entity`
    /`find_paths`: there is none to clamp. `edge_id` is a single primary-key
    lookup (`Edge.objects.get(pk=...)`, O(1) via the primary-key index, not
    proportional to table size), and `edge.attestations.all()` is bounded by
    how many independent sources this project ingests today (a handful) --
    not by any value the caller passes in.

    Raises `Edge.DoesNotExist` if `edge_id` does not resolve to a row.
    """
    edge = Edge.objects.select_related("source_entity", "target_entity").get(pk=edge_id)
    attestations = [
        {
            "attestation_id": a.id,
            "source_name": a.source_name,
            "source_url": a.source_url,
            "observed_at": a.observed_at.isoformat() if a.observed_at else None,
            "snapshot_ref": a.snapshot_ref,
            "match_confidence": a.match_confidence,
            "match_method": a.match_method,
        }
        for a in edge.attestations.all()
    ]
    return {
        "edge_id": edge.id,
        "edge_type": edge.edge_type,
        "source_entity": entity_summary(edge.source_entity),
        "target_entity": entity_summary(edge.target_entity),
        "valid_from": edge.valid_from.isoformat() if edge.valid_from else None,
        "valid_to": edge.valid_to.isoformat() if edge.valid_to else None,
        "attestations": attestations,
    }


_COVERAGE_UNIVERSES = ("all", "procurement_supplier")


def coverage_report(universe: str = "all") -> dict[str, Any]:
    """Officer-roster coverage, reusing `ch_officers`'s own report functions.

    `universe="all"` -> `ch_officers.coverage_report()` (every `GB-COH`
    company Entity already in the graph). `universe="procurement_supplier"`
    -> `ch_officers.procurement_universe_coverage_report()`, scoped to the
    benchmark-independent procurement-supplier universe. No coverage
    computation is reimplemented here.

    Audited alongside `resolve_entity`/`find_paths` for an unbounded
    caller-supplied query: `universe` is the only input, it is a closed set
    of two literal strings already exhaustively validated below (anything
    else raises `ValueError`), not a numeric bound -- there is nothing here
    to clamp. The underlying aggregate scans in `ch_officers` are pre-existing
    code this server only calls, out of this pass's edit scope.
    """
    if universe == "all":
        return ch_officers.coverage_report()
    if universe == "procurement_supplier":
        return ch_officers.procurement_universe_coverage_report()
    raise ValueError(f"unknown universe {universe!r}; expected one of {_COVERAGE_UNIVERSES}")


def list_sources() -> list[dict[str, Any]]:
    """The connector register: every `sources/*.yml` entry.

    Each entry carries locale, registry schemes, data class, `dpia_cleared`,
    and licence/redistribution terms -- reads `sources/` via the same
    `register.loader.all_sources()` every connector's own register-refusal
    check uses, so this can never drift from what a connector actually
    resolves at runtime.
    """
    return [source.model_dump(mode="json") for source in all_sources()]


def describe_pipeline() -> str:
    """The country-onboarding contract, verbatim from `sources/README.md`.

    Returns the raw file contents rather than a paraphrase so this tool can
    never drift from the actual documented contract.
    """
    return _README_PATH.read_text(encoding="utf-8")
