# AGENTS.md — Decorruptio

## Project overview
Open-source public-finance transparency platform: follow public money, surface
**risk indicators for human investigators — never verdicts of guilt**. Read the
[README](README.md) first, especially *Status, honestly* — Phase 1 produced a
**measured null** on UK retrospective bid-rigging detection, and the coverage gate
currently emits `NO SCORE`. Do not write code that implies otherwise.

Design docs (system spec, ADR-000…ADR-009, research + null write-ups) live in a
separate private Obsidian vault, not this repo. Docstrings citing
`02 Projects/Ideas/Decorruptio/...` are vault paths.

## Architecture (ADR-002, ADR-003, ADR-005)
- **Six layers**: connectors → staging → indicators → flags → review → publication.
- **Django ORM + PostgreSQL** staging (JSONB + hashed local files). *Not* DuckDB /
  Parquet, *not* Aleph / FollowTheMoney / nomenklatura — models are FtM-*shaped* only.
- **Scheduling is cron + Django management commands.** No Dagster, no MinIO, no
  object store, no Vite/React dashboard (retired per ADR-005 D6; `dagster.yaml` and
  `dashboard/` are leftovers pending deletion — do not build on them).
- **Connector protocol**: `Connector` — discover + fetch raw payloads only, no parsing.
- **Indicators**: `Indicator` ABC — yield `Flag` objects with provenance + version stamp.
- **Graph layer**: `Entity` / `Alias` / `Edge` / `Attestation`. Cross-register identity
  is **asserted as an Attestation with confidence, never merged** (ADR-006).
- **Fail-closed measurement boundary** (ADR-008): a blocked coverage/stratum gate
  emits a no-score certificate bound to a code commit + graph hash. **Unknown must
  never read as pass.**
- **Register**: `sources/*.yml` (legal basis, license, redistribution, A1/A2, tier)
  + `locales/*.yml` (procedure metadata).

## Key paths
- `src/uncorrupt/core/` — provenance, version stamps, data classes, tiers, FX, circuit breaker
- `src/uncorrupt/connectors/` — procurement connectors (base.py, registry.py, conformance)
- `src/uncorrupt/staging/` — Django models, ingest mappers, Companies House, aliases
- `src/uncorrupt/graph/` — Entity/Alias/Edge/Attestation, identity + GLEIF resolution, benchmark
- `src/uncorrupt/gates/` — coverage.py, stratum.py, certificate.py, binding.py (ADR-008)
- `src/uncorrupt/indicators/catalog/` — i001–i008 implementations
- `src/uncorrupt/extraction/` — PDF / HTML / OCR extraction + quality gate
- `src/uncorrupt/mcp/` — read-only MCP server (server.py, tools.py, privacy.py)
- `src/uncorrupt/vault/` — keyed-HMAC tokenizer (refuses to run without a key)
- `sources/*.yml` · `locales/*.yml` — source register + locale profiles
- `tests/guardrails/` — ADR-000 encoded as executable tests
- `tests/fixtures/*_controls.json` — externally specified ground truth (named real people
  from public registers; see the README's personal-data note before adding rows)
- `scripts/` — production-quality runners only (ingest, measure, controls, benchmark)

## Build & quality gates
```bash
uv sync --extra dev --extra pdf
uv run ruff check . && uv run ruff format --check .
uv run mypy                  # strict on core/, register/, vault/
uv run pytest                # 1101 tests, incl. the guardrail suite
```
`--extra pdf` is **not optional for the test suite**: `pypdf` is imported at module
scope by `tests/extraction/pdf_fixtures.py` and `tests/research/test_citation_verifier.py`,
so `uv sync --extra dev` alone fails at collection with three errors. CI installs
`--extra dev --extra pdf` for this reason.

All four gates are green on `main`. Keep them that way root-cause — do not add
blanket `# type: ignore` to restore a green run.

## Conventions
- Indicators yield `Flag` objects — never verdicts (ADR-000 G4).
- Every flag carries `ProvenanceRecord` with source URL + content hash + license.
- Indicators are `UNVALIDATED` by default; must be `VALIDATED` per locale to run.
  i001–i005 are registered as `pyproject.toml` entry points; **i006–i008 are
  implemented and tested but unregistered**, so `load_indicators()` misses them.
- Connectors are ingestion-only — no parsing (ADR-001 D1).
- `A2` (public-persons) data requires `dpia_cleared: true` in the source register or
  the connector refuses to load. Layer B (flagging) is gated per-jurisdiction.
- **Monetary values are integer cents. Never floats for money.**
- Django: generate migrations, never apply them — `migrate` is human-only.

## The absence rule (non-negotiable — this is how the project got burned seven times)
Any conclusion of the form "count = 0 / no matches / does not exist" that **closes a
line of work** must ship with:
1. **a positive control** — the same probe run against something known present, and
2. **the verbatim command** used.

A passing positive control proves the probe can find *one* thing, **not** that an
enumeration is complete. For enumeration claims, add an independent count or pick a
control member specifically because it is hard (line-wrapped, differently cased,
boundary-adjacent). Seven documented false absences came from skipping this.

## Scripts
- **One script, iterated; never a new variant per attempt.** Delete exploratory
  scripts before opening a PR. Throwaway experiments go in `experiments/` (gitignored,
  except the force-tracked `no_score_certificate.json`).
- Some scripts (`measure_temporal_lift.py`, `phase_c_paths.py`, `cohort_test_v2.py`)
  read a local cohort CSV that is not in the repo and will refuse to run without it.

## API notes
- **UK Companies House**: needs `COMPANIES_HOUSE_API_KEY`. The search endpoint requires
  a query term and refuses `start_index` beyond ~5000 — population figures need the
  bulk product. `company_names` can pack **multiple companies into one string**
  (`"X LIMITED (01892133)  Y LIMITED (00686405)"`) — parse accordingly.
- **UK Parliament Interests API**: `interests-api.parliament.uk`, exposes its own
  `totalResults` — use it as the coverage denominator.
- **UK Contracts Finder**: native OCDS 1.1, cursor pagination via `Link` header.
- **ProZorro**: `api.openprocurement.org/api/2.5/tenders` — oldest first, use offset.
- **Colombia SECOP II**: Socrata API, SoQL syntax (`$where`, `$limit`, `$order`).
- Lords register: served behind a Cloudflare browser challenge; a captured frozen
  snapshot backs the control battery.

## Git
Branch + PR for every change; never commit directly to `main`, and never commit
because it seems convenient — an agent doing that unreviewed has already cost this
project one unauthorized push to `main`.
