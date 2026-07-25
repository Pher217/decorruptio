# AGENTS.md — Decorruptio

## Project overview
Open-source public-finance transparency / anomaly-detection platform. Comparative kill experiment: 10 reproducible flags from procurement data → 3 journalists blind-rate → ≥1 says "I'd chase this" or kill.

Three countries: Ukraine (ProZorro), Colombia (SECOP II), UK (Contracts Finder).

## Architecture (ADR-002)
- **Data-first, deep-narrow**: DuckDB/Parquet staging, not Aleph/FtM
- **Connector protocol**: `Connector` Protocol — discover + fetch raw payloads only
- **Staging layer**: DuckDB unified OCDS-flattened schema (tenders, awards, bids)
- **Indicators**: `Indicator` ABC — yield `Flag` objects with provenance + version stamp
- **Register**: `sources/*.yml` (legal basis) + `locales/*.yml` (procedure metadata)

## Key paths
- `src/uncorrupt/connectors/` — ingestion connectors (base.py, registry.py)
- `src/uncorrupt/staging/` — DuckDB schema, ingest mappers, query helpers
- `src/uncorrupt/indicators/catalog/` — i001-i005 implementations
- `sources/*.yml` — source registry (A1/A2, license, redistribution)
- `locales/*.yml` — locale profiles (min bid days, direct award thresholds)
- `scripts/` — smoke test, e2e test, kill experiment runner

## Build & quality gates
```bash
uv run ruff check .          # lint
uv run ruff format .         # format
uv run mypy                  # type check
uv run pytest -q             # tests (13 pass)
```

## Running the kill experiment
```bash
uv run python scripts/smoke_test_connectors.py     # verify APIs live
uv run python scripts/e2e_staging_test.py           # fetch→ingest→query
uv run python scripts/kill_experiment.py --sample-size 50 --output flags.json
```

## Conventions
- Indicators yield `Flag` objects — never verdicts (ADR-000 G4)
- Every flag carries `ProvenanceRecord` with source URL + content hash + license
- Indicators are `UNVALIDATED` by default; must be `VALIDATED` per locale to run
- Connectors are ingestion-only — no parsing (ADR-001 D1)
- `A2` data requires `dpia_cleared: true` in source registry or connector refuses to load

## API notes
- **ProZorro**: `api.openprocurement.org/api/2.5/tenders` — returns oldest first, use offset for recent
- **UK Contracts Finder**: native OCDS 1.1, cursor pagination via `Link` header
- **Colombia SECOP II**: Socrata API, SoQL query syntax (`$where`, `$limit`, `$order`)

## Scripts
- **One script, iterated; never a new variant per attempt.** Delete exploratory scripts before opening a PR.
- Keep `scripts/` to production-quality runners only. Throwaway experiments go in `experiments/` (gitignored).

## Mexico dropped
CompraNet API returning 503/500. Replaced with Colombia SECOP II (same enforcement-gap profile, live API).