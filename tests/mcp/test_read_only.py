"""Proves the MCP tool layer cannot mutate the graph -- ADR-004's hard
boundary (no LLM in the measurement path) depends on this holding, not just
today but as the module evolves.

Three independent guarantees, deliberately redundant:

1. Static: `tools.py`'s source contains no call to any ORM write method.
2. Static: `tools.py`'s source contains no `try`/`except` -- a
   caller-facing tool must raise on a real failure, never swallow it into
   an empty result indistinguishable from "no data" (the anti-pattern a
   sibling review of another project's MCP server flagged; this project's
   `Entity.DoesNotExist`/`Edge.DoesNotExist`/`ValueError` raises are the
   confirmed-correct alternative -- see the `*_raises_does_not_exist`/
   `*_raises_value_error` tests in `test_tools.py`).
3. Dynamic: calling every tool leaves row counts on every graph table
   unchanged.
"""

from __future__ import annotations

import inspect
import re

import pytest

from uncorrupt.graph.models import Alias, Attestation, Edge, Entity
from uncorrupt.mcp import tools

_MUTATING_CALL_PATTERN = re.compile(
    r"\.(save|create|update|delete|bulk_create|bulk_update|get_or_create|update_or_create)\s*\("
)
_TRY_EXCEPT_PATTERN = re.compile(r"^\s*(try\s*:|except\b)", re.MULTILINE)


class TestNoStaticMutationCalls:
    def test_tools_source_contains_no_orm_write_method_call(self):
        """GIVEN the tools.py source WHEN scanned for ORM write-method call
        patterns (.save(, .create(, .update(, .delete(, .bulk_create(,
        .bulk_update(, .get_or_create(, .update_or_create() THEN none are
        found."""
        source = inspect.getsource(tools)
        match = _MUTATING_CALL_PATTERN.search(source)
        assert match is None, f"found a mutating ORM call pattern: {match}"

    def test_privacy_source_contains_no_orm_write_method_call(self):
        """GIVEN the privacy.py source WHEN scanned for the same patterns
        THEN none are found either -- the Entity -> dict boundary is also
        pure read."""
        from uncorrupt.mcp import privacy

        source = inspect.getsource(privacy)
        match = _MUTATING_CALL_PATTERN.search(source)
        assert match is None, f"found a mutating ORM call pattern: {match}"


class TestNoExceptionSwallowing:
    """Raise-don't-swallow: a failed query must never look like an empty
    result to the caller."""

    def test_tools_source_contains_no_try_except_block(self):
        """GIVEN the tools.py source WHEN scanned for `try`/`except` THEN
        none is found -- every failure (a missing id, a malformed register
        entry, an invalid parameter) propagates as a real exception rather
        than being caught and turned into `[]` or `None`."""
        source = inspect.getsource(tools)
        match = _TRY_EXCEPT_PATTERN.search(source)
        assert match is None, f"found a try/except block: {match}"


@pytest.mark.django_db
class TestNoDynamicMutation:
    def _row_counts(self) -> tuple[int, int, int, int]:
        return (
            Entity.objects.count(),
            Edge.objects.count(),
            Attestation.objects.count(),
            Alias.objects.count(),
        )

    def _seed_graph(self) -> tuple[Entity, Entity, Edge]:
        referrer = Entity.objects.create(entity_type="person", name="Referrer MP")
        supplier = Entity.objects.create(
            entity_type="company",
            name="Supplier Ltd",
            registry_scheme="GB-COH",
            registry_id="12345678",
            company_number="12345678",
        )
        edge = Edge.objects.create(
            edge_type="referred_to_lane",
            source_entity=referrer,
            target_entity=supplier,
            valid_from="2020-04-01",
        )
        Attestation.objects.create(edge=edge, source_name="DHSC High Priority Lane")
        return referrer, supplier, edge

    def test_calling_every_tool_leaves_row_counts_unchanged(self):
        """GIVEN a populated graph WHEN every read-only tool is called
        (resolve_entity, get_entity, find_paths, get_attestations,
        coverage_report, list_sources, describe_pipeline) THEN Entity,
        Edge, Attestation, and Alias row counts are identical before and
        after."""
        referrer, supplier, edge = self._seed_graph()
        before = self._row_counts()

        tools.resolve_entity(name="Supplier")
        tools.get_entity(referrer.id)
        tools.find_paths(referrer.id, supplier.id, max_hops=1)
        tools.get_attestations(edge.id)
        tools.coverage_report()
        tools.list_sources()
        tools.describe_pipeline()

        after = self._row_counts()
        assert after == before
