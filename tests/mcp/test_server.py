"""Tests for `uncorrupt.mcp.server` -- tool registration and its read-only stance.

These assert the server-level guarantees a client actually sees: which
tools exist, that every one is annotated read-only/non-destructive, and
that the surfaced description states the ADR-004 boundary -- distinct from
`test_read_only.py`, which proves the *implementation* never mutates.
"""

from __future__ import annotations

from uncorrupt.mcp import tools as tools_module
from uncorrupt.mcp.server import DESCRIPTION, build_server

EXPECTED_TOOL_NAMES = {
    "resolve_entity",
    "get_entity",
    "find_paths",
    "get_attestations",
    "coverage_report",
    "list_sources",
    "describe_pipeline",
}


class TestServerToolRegistration:
    def test_registers_exactly_the_expected_tool_names(self):
        """GIVEN a freshly built server WHEN its tools are listed THEN the
        set of registered tool names matches exactly the seven exploration
        tools -- no more, no fewer."""
        server = build_server()
        registered = {t.name for t in server._tool_manager.list_tools()}
        assert registered == EXPECTED_TOOL_NAMES

    def test_every_tool_is_annotated_read_only(self):
        """GIVEN a freshly built server WHEN its tools are listed THEN every
        tool's annotations declare readOnlyHint=True."""
        server = build_server()
        for tool in server._tool_manager.list_tools():
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True

    def test_every_tool_is_annotated_non_destructive(self):
        """GIVEN a freshly built server WHEN its tools are listed THEN every
        tool's annotations declare destructiveHint=False."""
        server = build_server()
        for tool in server._tool_manager.list_tools():
            assert tool.annotations.destructive_hint is False

    def test_registered_tool_functions_are_the_tools_module_functions(self):
        """GIVEN a freshly built server WHEN its tools are listed THEN each
        registered tool's underlying function is the exact function object
        from `tools.py` -- the server wraps, it does not reimplement."""
        server = build_server()
        by_name = {t.name: t.fn for t in server._tool_manager.list_tools()}
        assert by_name["resolve_entity"] is tools_module.resolve_entity
        assert by_name["find_paths"] is tools_module.find_paths


class TestServerDescription:
    def test_description_states_the_read_only_boundary(self):
        """GIVEN the server description surfaced to clients WHEN read THEN
        it states the server is read-only and cites ADR-004."""
        assert "ADR-004" in DESCRIPTION
        assert "read-only" in DESCRIPTION.lower() or "read only" in DESCRIPTION.lower()

    def test_description_states_candidates_never_a_single_guess(self):
        """GIVEN the server description WHEN read THEN it states that entity
        resolution returns candidates rather than a single guessed match."""
        assert "candidates" in DESCRIPTION.lower()
