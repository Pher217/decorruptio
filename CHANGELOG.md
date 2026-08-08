# Changelog

All notable changes to Decorruptio are documented here. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/). There are no tagged releases
yet — the project is pre-1.0 (`version = "0.0.1"`) and everything below has
landed directly on `main`.

## [Unreleased]

### Phase 1 — relationship-recovery benchmark

The pre-registered Phase C gold-manifest benchmark (`scripts/run_gold_benchmark.py`)
is built to return exactly one verdict — CONFIRMED / PARTIAL / REFUTED /
INSTRUMENT-LIMITED — gated by ADR-008's fail-closed measurement boundary: if
a required coverage or per-stratum control-battery gate has not been measured
or has failed, the benchmark refuses to emit a verdict at all and instead
writes a no-score certificate naming exactly which gates blocked it
(`src/uncorrupt/gates/certificate.py`). The checked-in
`experiments/no_score_certificate.json` is exactly that outcome: as of its
`issued_at` timestamp it records `"verdict": "NO SCORE -- INSTRUMENT-LIMITED"`
against five named blockers — three per-stratum control batteries (Commons,
Lords, Companies House officer appointments) and two coverage families
(Companies House officer roster, Commons register) — so Phase 1 produced a
measured null result: an auditable "we did not score," not a positive or
negative finding.

### Stack

- **Migrated from DuckDB/Parquet staging to Django 5.2 + PostgreSQL** (`9b4ed27`).
  Staging moved from raw DuckDB SQL to Django ORM models (`Tender`, `Award`,
  `Bid`, `Flag`, all monetary values as integer cents), indicators i001–i005
  moved from DuckDB SQL to ORM queries, and CI gained a PostgreSQL 16 service
  container. `docker-compose.yml`'s MinIO service was removed in the same
  commit.
- Dagster and the Vite/React dashboard scaffolded at project init
  (`e19ba50`) were dropped from the architecture per ADR-005 D6 — the
  project schedules via cron + Django management commands instead (README).

### Graph layer

- **FtM-shaped relationship graph models — `Entity`, `Alias`, `Edge`**
  (`8def27a`).
- **`Attestation` model** (`d9e107b`): source citation fields moved off
  `Edge` onto a new `Attestation` model (FK to `Edge`, unique on
  `(edge, source_name, source_reference)`), with bitemporal fields
  (`observed_at`, `snapshot_ref`, `derived_from`) — `Edge` is the claim,
  `Attestation` is the evidence, so corroboration becomes countable and
  source laundering becomes detectable.
- **Cross-register identity resolution as assert-not-merge** (`f9d8543`,
  ADR-006 "duplicate over merge"): a person appearing under both a
  `UK-PARLIAMENT-MEMBER` node and a `GB-COH-OFFICER` node is linked with a
  `same_as` `Edge` carrying its own `Attestation`, never merged into one
  entity — merging would misrepresent the registries, which themselves
  sometimes issue one person multiple officer IDs.

### Data sources

- **EC donations** ingester — Phase 1.2 (`bf29d38`).
- **UK Parliament register of interests** ingester — Phase 1.3 (`9c09c8b`).
- **Companies House officers** ingester — Phase 1.4 (`866a038`).
- **Lords register** ingest, alongside the `Attestation` model — Phase 1.5
  (`d9e107b`).
- **GLEIF LEI ingest** for global company coverage (`e33f879`).
- **Register of Overseas Entities (ROE)** connector (`4b072c1`).
- **OpenSanctions** non-personal entity slice (Company/Organization/LegalEntity)
  (`b313e0e`) — the source register entry (`sources/opensanctions.yml`) ships
  `data_class: A2`, `redistribution: non_commercial`, `dpia_cleared: false`
  from day one so the A2/redistribution guardrail tests have a real case; the
  personal-data (PEP/sanctions) slice is not ingested in Phase 1.
- **Officer appointments, second hop** (`eef1b8a`), later cross-checked
  against disqualified-director data (`249db4b`).

### Indicators

- i001–i005 shipped with the initial DuckDB-era scaffold, then ported to
  Django ORM queries in the stack migration (`9b4ed27`).
- **i006 (incorporation proximity), i007 (value vs. company size), i008
  (dormancy/delinquency)** added alongside the Companies House join and
  entity resolution (`e6caf16`).
- Indicators are disabled-unless-validated: `enabled_for(locale)` returns an
  indicator only where its own `validation` map marks that locale
  `VALIDATED`. Note that i001–i008 each declare `"gb": VALIDATED`, a claim
  that predates the Phase-1 null and is not defensible at the power the null
  measured.
- i006–i008 are implemented and tested but **not registered** under the
  `uncorrupt.indicators` entry-point group, so `load_indicators()` does not
  return them; `scripts/kill_experiment.py` imports them directly. See the
  README's Indicators section for why registering them is a decision rather
  than a packaging fix.

### Document extraction & MCP

- **Document-extraction layer** — text layer first, OCR quality-gated
  fallback (`81109b0`), later hardened with an `EXTRACTION_UNRELIABLE`
  status so a JS-rendered fetch is never reported ABSENT (`24b9ec9`).
- **Read-only MCP server over the FollowTheMoney-shaped graph** (`3de6953`),
  with caller-supplied `limit`/`max_hops` clamped (`87119fe`).

### Measurement gates (ADR-008)

- **Fail-closed 100%-accounted coverage gate** (spec A2.4.2) and
  **per-material-stratum retrieval/temporal gate** (spec A2.4.3) —
  `src/uncorrupt/gates/coverage.py`, `src/uncorrupt/gates/stratum.py`.
- **No-score certificate** naming every blocking gate by name, so "we did
  not score" is auditable rather than discretionary — `src/uncorrupt/gates/certificate.py`.
- **Freeze-state binding**, including an attestation-inclusive graph hash
  and a control-fixtures hash, so a gate artifact can't silently bind to a
  graph or fixture state it never actually measured — `src/uncorrupt/gates/binding.py`
  (`1db3952` fixed a `None`-vs-`date` sort crash in the hash and first
  emitted the certificate; `39a6b72` closed a gap where the coverage-gate
  family could go unmeasured without producing a blocker; `47aa2d3` began
  tracking the certificate as a permanent artifact).

### Fixed

- WSGI/ASGI entrypoints defaulted to SQLite instead of PostgreSQL (`1233e0f`).
- Several fail-open gaps in identity-match confidence reporting, closed
  across a multi-commit review pass (`728ce7c`, `5008f74`, `8cc591f`,
  `b566593`).
