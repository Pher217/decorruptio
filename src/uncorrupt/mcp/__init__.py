"""MCP server over the FollowTheMoney graph -- exploration/orientation only.

Gives an LLM agent (or a human investigator working alongside one) the
ability to look around the graph -- resolve a company or official, walk
short paths between two entities, inspect the evidence behind a claim,
check officer-roster coverage, and read the connector register -- in a
handful of tool calls instead of reading the codebase. Because it sits on
the FtM-shaped model (`Entity`/`Edge`/`Attestation`) and the `sources/` +
`locales/` register rather than on any one country's tables, it is
country-agnostic by construction: onboarding Brazil or Colombia does not
require a new MCP tool.

Hard boundary (ADR-004: no LLM in the measurement path). This package is
READ-ONLY and exists for exploration, never scoring:

- Every tool in `tools.py` only ever reads (`.filter`/`.get`/`.values*`/
  `.annotate`) -- there is no mutation tool, and none of these functions
  ever calls `.save`/`.create`/`.update`/`.delete`/`.bulk_create`/
  `.bulk_update`/`.get_or_create`/`.update_or_create` (enforced by a static
  source-scan test, `tests/mcp/test_read_only.py`).
- It never imports `uncorrupt.graph.benchmark`, `scripts/run_gold_benchmark.py`,
  or any other verdict/scoring code, and must never be wired into the
  scoring pipeline. The project's scientific value rests on the benchmark
  measurement being deterministic and pre-registered; a tool that could
  quietly influence it would destroy that.
- `resolve_entity` returns CANDIDATES, never a single guessed match --
  resolution is by registry identifier wherever possible, never by name
  string alone (ADR-004 D2), and ties are surfaced, not silently narrowed
  (the project's "duplicate over merge" principle, ADR-006).
- Person-level data stays inside ADR-004 D1's scope boundary: company-level
  entities and public-function officials only. `privacy.entity_summary` is
  the single sanctioned `Entity -> dict` boundary every tool goes through;
  it never serialises `Entity.properties` (the one field not enforced
  clean-by-construction the way allowlisted ingest fields are), so DOB,
  nationality, and address can never surface even if a future ingest bug
  put them there.

See `README.md` in this package for how to register the server with an MCP
client and a worked example.
"""

from __future__ import annotations
