# Uncorrupt

**Follow the world's public money. Surface anomalies for human investigators.
Publish reproducible transparency data.** Risk indicators for investigation —
**never verdicts of guilt.**

> **Design docs (spec, ADRs, research) live in a separate Obsidian vault, not this
> repo.** This repo is code + config. The guardrails summary below is the part you
> must not skip.

## Status

Phase 1 — building the **relationship-recovery benchmark** (spec v0.2). The four
data-source ingesters (EC donations, Parliament interests, Companies House
officers, Lords register) are live and write into `Entity` / `Edge` /
`Attestation` graph models. Indicators i001–i005 and the connectors for Ukraine,
Colombia, and UK procurement are also in place. No Dagster, no MinIO, no
Vite/React dashboard — those were dropped per ADR-005 D6.

## The guardrails are executable, not prose

This project deliberately does **not** algorithmically "expose corrupt
politicians" — that is illegal (GDPR/LGPD), defamation-exposed, and unsound
(automated flagging is a triage funnel: the best deployed system turns ~17,700
flags into ~134 actions). So the guardrails are enforced in code and CI:

- **`sources/`** — a machine-readable legal-basis + redistribution register. A
  connector **cannot run** without a valid entry. `opensanctions.yml` ships marked
  `non_commercial` + `A2` + `dpia_cleared: false` so the guardrail tests have a
  real case.
- **`tests/guardrails/`** — CI fails if a tier-b/c field reaches a tier-a export,
  if a non-redistributable source leaks into a bulk open export, if a flag lacks
  provenance, or if a raw national ID is serialized.
- **`src/uncorrupt/core/`** — provenance + version stamps, A1/A2/B data classes,
  publication tiers, composite company key, FX-at-date. Everything imports this.
- **`src/uncorrupt/vault/`** — keyed-HMAC tokenizer; refuses to run without a key,
  never returns a raw ID.

## Architecture (6 layers → Django ORM + cron)

```
L1 Connectors (httpx fetch)
  → L2 Staging (Django ORM → PostgreSQL: Postgres JSONB + hashed local files)
  → L3 Indicators (Django ORM queries; Indicator ABC, i001–i005)
  → L4 Flags (Django model with provenance + version stamp)
  → L5 Review Workspace (DRF API + Django admin)
  → L6 Publication (tiered: open data export / vetted feed)
```

Scheduled via cron + Django management commands — no Dagster, no MinIO, no
object store. FtM-*shaped* models live in the Django ORM, but the project does
**not** depend on the FollowTheMoney library, Aleph, or nomenklatura, and OCP
indicators are not used (the project ships its own `Indicator` ABC with
i001–i005). Two first-class extension points: **connectors**
(`src/uncorrupt/connectors/`) and **indicators**
(`src/uncorrupt/indicators/`), both discovered via `pyproject.toml` entry points.

## Scaling split (why the layout looks like this)

- **A1** — non-personal money data (contracts/budgets/companies): scales globally.
  **Phase 1 builds only A1.**
- **A2** — public-persons data (PEPs/office-holders/sanctions/BO): gated by an
  up-front global DPIA; `A2` connectors refuse to load until `dpia_cleared: true`.
- **B** — flagging & investigation: gated per-jurisdiction by a partner + legal
  opinion.

## Quickstart

```bash
uv sync --extra dev                      # install (Python 3.12)
uv run uncorrupt validate-registry       # load + validate sources/*.yml
uv run ruff check .                      # lint
uv run ruff format --check .             # format check
uv run mypy                              # type check
uv run pytest -q                         # runs the guardrail + unit suite
```

Postgres is optional for the test suite (tests use the Django test settings);
`make up` starts a local Postgres via `docker compose` if you want to run the
ingesters against a real database. See the `Makefile` for the full target list.

## Phase-1 focus — the relationship-recovery benchmark

Ingest four data sources — **EC donations**, **Parliament interests**, **CH
officers**, and the **Lords register** — into `Entity` / `Edge` / `Attestation`
graph models, then demonstrate that the graph can recover independently
substantiated relationships:

- Build **10–20 independently substantiated positive relationships** (edges
  corroborated by ≥2 attestations from different sources).
- Score **atomic claims**: precision/recall on edge recovery, temporal accuracy
  (`Edge.valid_from`/`valid_to` vs. the attested real-world window), and
  resolution rates (how often a donor/counterparty resolves to a real company
  without a guess).

This supersedes the old "kill experiment" Phase-1 DoD (`make up && make demo`
running Dagster end-to-end), which spec v0.2 retired along with Dagster, MinIO,
and the Vite/React dashboard.

## Stack

Django 5.2 + Django REST Framework · PostgreSQL (Postgres JSONB + hashed local
files; **monetary values as integer cents, never floats**) · httpx for HTTP
fetching · cron + Django management commands for scheduling · Django admin +
DRF API for review/publication. Lint/format: ruff. Types: mypy + django-stubs.
Tests: pytest + pytest-django.

FtM-*shaped* models in the Django ORM — **not** a dependency on the
FollowTheMoney library, Aleph, or nomenklatura. OCP indicators are not used; the
project has its own `Indicator` ABC (i001–i005). Splink-style entity resolution
is Phase 2.

## License

Code: **MIT**. **Data is not code** — third-party source data carries its own terms
(tracked per source in `sources/*.yml`); some (e.g. OpenSanctions, CC-BY-NC) are
excluded from bulk open exports. See `LICENSE` and `TRADEMARKS.md`.

---

*Not legal advice. No personal-data processing before a written legal opinion per
jurisdiction and the A2 DPIA.*
