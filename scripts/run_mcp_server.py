"""Run the FollowTheMoney graph MCP server over stdio.

Exposes the read-only exploration/orientation layer in `src/uncorrupt/mcp/`
to an MCP client (Claude Desktop, Claude Code, or any other MCP-speaking
agent) over stdio. See `src/uncorrupt/mcp/README.md` for how to register
this with a client and a worked example (resolve a company, find paths to
an official, inspect the attestations).

ADR-004 hard boundary: this server is READ-ONLY exploration/orientation for
a human investigator plus an assistant -- no LLM in the measurement path. It
exposes no mutation tools, cannot score the benchmark or produce verdicts,
and must never be wired into the scoring pipeline
(`scripts/run_gold_benchmark.py` and friends). See `src/uncorrupt/mcp/server.py`
for the description surfaced to clients and `src/uncorrupt/mcp/tools.py` for
what each tool actually does.

Usage:
    PYTHONPATH=.:src python scripts/run_mcp_server.py
"""

from __future__ import annotations

import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.mcp.server import server  # noqa: E402


def main() -> None:
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
