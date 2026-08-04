# `uncorrupt.mcp` — MCP server over the FollowTheMoney graph

An [MCP](https://modelcontextprotocol.io) server that lets an LLM agent (or a
human investigator working alongside one) explore the relationship graph
(`Entity`/`Edge`/`Attestation`) without reading the codebase: resolve a
company or official, walk short paths between two entities, inspect the
evidence behind a claim, check officer-roster coverage, and read the
connector register — in a handful of tool calls.

Because it sits on the FtM-shaped model and the `sources/`/`locales/`
register rather than on any one country's tables, it is **country-agnostic
by construction** — onboarding a new country's connectors does not require a
new MCP tool.

## Hard boundary — read this before wiring it up

**No LLM in the measurement path (ADR-004).** This server is for
**exploration and orientation only**. Every tool is read-only (no tool ever
calls `.save`/`.create`/`.update`/`.delete`/`.bulk_create`/`.bulk_update`/
`.get_or_create`/`.update_or_create` — enforced by a static source scan in
`tests/mcp/test_read_only.py`, not just convention). It cannot score the
benchmark, produce verdicts, or be reachable from the scoring pipeline
(`scripts/run_gold_benchmark.py` and friends). The project's scientific
value rests on the benchmark measurement being deterministic and
pre-registered — a tool that could quietly influence it would destroy that.

**Privacy (ADR-004 D1).** Person-level output is limited to registry
identifier, name, and public-function role (e.g. "MP for X") — never date of
birth, nationality, or address, even if a future ingest bug wrote one of
those into the database (`privacy.entity_summary` never reads
`Entity.properties`, the one field not enforced clean by a DB constraint —
see `tests/mcp/test_privacy.py`).

**Resolution, never guessing (ADR-004 D2 / ADR-006).** `resolve_entity`
always returns a list of candidates. It never picks one for you, even when
there is exactly one strong match by name — ties are surfaced, not merged.

**Caller-supplied bounds are clamped server-side, silently but visibly.**
Against a ~288k-row `Entity` table and a 400k+-edge graph, `resolve_entity`'s
`limit` and `find_paths`'s `max_hops` are both requests, not grants: each is
clamped to a hard ceiling (`limit` to 200, `max_hops` to 4 — the locked Phase
C benchmark only ever needs 2) and enforced in the ORM query itself
(`qs[:effective_limit]`), never by materializing the full match set and
slicing afterwards. The clamp is silent-proof, not silent: both tools return
the *effective* value they used (`resolve_entity`'s `limit` key,
`find_paths`'s `max_hops` key), so a caller that asked for more can see it
was capped.

## Tools

| Tool | Signature | Returns |
|---|---|---|
| `resolve_entity` | `(name=None, company_number=None, registry_scheme=None, registry_id=None, entity_type=None, limit=20)` | `{"limit": <effective value, ≤200>, "candidates": [...]}` — candidates, never a single guess |
| `get_entity` | `(entity_id)` | The entity plus its edge counts by type |
| `find_paths` | `(source_id, target_id, max_hops=2)` | Paths ≤ effective `max_hops` (clamped to ≤4), each edge's type/`valid_from`/attesting sources |
| `get_attestations` | `(edge_id)` | Every attestation on that edge: source name, URL, `observed_at`, `snapshot_ref` |
| `coverage_report` | `(universe="all")` | Officer-roster coverage tiers (`"all"` or `"procurement_supplier"`) |
| `list_sources` | `()` | Every `sources/*.yml` entry: locale, registry schemes, data class, `dpia_cleared`, licence |
| `describe_pipeline` | `()` | The country-onboarding contract, verbatim from `sources/README.md` |

See `tools.py` for full docstrings — in particular why `registry_scheme`
alone raises `ValueError` rather than dumping every entity in a register.

## Registering with Claude Desktop / Claude Code

Add an entry to your MCP client config (`claude_desktop_config.json` for
Claude Desktop, or `.mcp.json` at the repo root for Claude Code), pointing at
the repo's virtualenv Python and `scripts/run_mcp_server.py`:

```json
{
  "mcpServers": {
    "uncorrupt-graph": {
      "command": "/absolute/path/to/decorruptio/.venv/bin/python",
      "args": ["scripts/run_mcp_server.py"],
      "cwd": "/absolute/path/to/decorruptio",
      "env": {
        "PYTHONPATH": ".:src",
        "DJANGO_SETTINGS_MODULE": "config.settings.base"
      }
    }
  }
}
```

Or run it directly to confirm it starts (it will block, waiting for a client
on stdin/stdout — `Ctrl-C` to stop):

```bash
PYTHONPATH=.:src python scripts/run_mcp_server.py
```

## Worked example

The transcript below is **real tool output**, captured by running the four
tools in sequence against a small illustrative fixture graph (not a live
database query, not a real case — `Jane Example MP` / `Example Supplies Ltd`
/ `Shared Consulting Ltd` are fixtures created for this walkthrough, in the
same relationship shape `scripts/phase_c_paths.py`'s own docstring
describes: a referrer connected to a supplier through a shared directorship,
with no direct edge between them). Company numbers are fixtures too — not
real Companies House registrations.

**1. Resolve the supplier by name:**

```python
resolve_entity(name="Example Supplies")
```
```json
{
  "limit": 20,
  "candidates": [
    {
      "entity_id": 3,
      "entity_type": "company",
      "name": "Example Supplies Ltd",
      "registry_scheme": "GB-COH",
      "registry_id": "09999002",
      "company_number": "09999002"
    }
  ]
}
```

**2. Resolve the official by name** (note: only `registry identifier`,
`name`, and `role_description` come back — no DOB, no address):

```python
resolve_entity(name="Jane Example")
```
```json
{
  "limit": 20,
  "candidates": [
    {
      "entity_id": 1,
      "entity_type": "person",
      "name": "Jane Example MP",
      "registry_scheme": null,
      "registry_id": null,
      "role_description": "MP for Example South"
    }
  ]
}
```

**3. Find paths between them** — there is no direct edge, but both are
officers of the same company:

```python
find_paths(source_id=1, target_id=3, max_hops=2)
```
```json
{
  "source_id": 1,
  "target_id": 3,
  "max_hops": 2,
  "paths": [
    {
      "hops": 2,
      "edges": [
        {
          "edge_id": 1,
          "edge_type": "officer_of",
          "source_entity_id": 1,
          "target_entity_id": 2,
          "valid_from": "2018-06-01",
          "attesting_sources": ["Companies House"]
        },
        {
          "edge_id": 2,
          "edge_type": "officer_of",
          "source_entity_id": 3,
          "target_entity_id": 2,
          "valid_from": "2019-02-10",
          "attesting_sources": ["Companies House"]
        }
      ]
    }
  ]
}
```

**4. Inspect the evidence behind the first edge:**

```python
get_attestations(edge_id=1)
```
```json
{
  "edge_id": 1,
  "edge_type": "officer_of",
  "source_entity": {
    "entity_id": 1,
    "entity_type": "person",
    "name": "Jane Example MP",
    "registry_scheme": null,
    "registry_id": null,
    "role_description": "MP for Example South"
  },
  "target_entity": {
    "entity_id": 2,
    "entity_type": "company",
    "name": "Shared Consulting Ltd",
    "registry_scheme": "GB-COH",
    "registry_id": "09999001",
    "company_number": "09999001"
  },
  "valid_from": "2018-06-01",
  "valid_to": null,
  "attestations": [
    {
      "attestation_id": 1,
      "source_name": "Companies House",
      "source_url": "https://find-and-update.company-information.service.gov.uk/company/09999001",
      "observed_at": "2026-01-15T00:00:00+00:00",
      "snapshot_ref": "aaaa...aaaa",
      "match_confidence": 1.0,
      "match_method": "identifier"
    }
  ]
}
```

Four tool calls: resolve the two ends by identifier, walk the path, read the
citation behind the claim that path is built from — no source code read, no
SQL written.
