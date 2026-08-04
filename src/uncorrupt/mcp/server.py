"""Builds the MCP server -- registers `tools.py`'s functions, nothing else.

This module contains no logic of its own: every tool it exposes is a plain
function from `tools.py`, registered as-is via `MCPServer.tool()(fn)`, and
every one of them is stamped `readOnlyHint=True` / `destructiveHint=False`
(`ToolAnnotations`) as a client-visible signal to match the enforced reality
-- there is no mutation tool to hide.
"""

from __future__ import annotations

from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from uncorrupt.mcp import tools

DESCRIPTION = """\
Read-only exploration/orientation layer over the FollowTheMoney-shaped
relationship graph (Entity/Edge/Attestation). For a human investigator plus
an assistant looking around the graph -- resolving a company or official,
walking short paths between two entities, inspecting the evidence behind a
claim, checking officer-roster coverage, and reading the connector register.

Hard boundary (ADR-004: no LLM in the measurement path): this server cannot
score the benchmark, produce verdicts, or mutate any data, and must never be
reachable from the scoring pipeline. Every tool here is a read. Person-level
output is limited to registry identifier, name, and public-function role --
never date of birth, nationality, or address (ADR-004 D1).

`resolve_entity` always returns candidates, never a single guessed match --
disambiguate by registry identifier, never by name string alone.
"""

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def build_server() -> MCPServer:
    mcp_server: MCPServer = MCPServer(
        name="uncorrupt-graph",
        description=DESCRIPTION,
        instructions=DESCRIPTION,
    )
    for fn in (
        tools.resolve_entity,
        tools.get_entity,
        tools.find_paths,
        tools.get_attestations,
        tools.coverage_report,
        tools.list_sources,
        tools.describe_pipeline,
    ):
        mcp_server.tool(annotations=_READ_ONLY)(fn)
    return mcp_server


server = build_server()
