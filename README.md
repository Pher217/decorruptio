# Uncorrupt

**Follow the world's public money. Surface anomalies for human investigators.
Publish reproducible transparency data.** Risk indicators for investigation —
**never verdicts of guilt.**

> **Design docs (spec, ADRs, research) live in a separate Obsidian vault, not this
> repo.** This repo is code + config. The guardrails summary below is the part you
> must not skip.

## Status

Phase-1 scaffold. The structure is complete and importable; most pipeline bodies
are stubs (`NotImplementedError`) with the interfaces and guardrails real. The
first thing to build is the **Phase-1 A1 walking skeleton** (see below).

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

## Architecture (7 layers → a Dagster DAG)

```
ingest (connectors) → staging (raw+clean) → normalize (OCDS + FtM + mapping)
  → resolve* → graph* → indicators → [HUMAN REVIEW GATE]* → publish (tier-a)
```
`*` = stubbed seam for a later phase (Phase 2–5). Two first-class extension points:
**connectors** (`src/uncorrupt/connectors/`) and **indicators**
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
uv sync --extra dev          # install (Python 3.12)
uv run uncorrupt validate-registry   # load + validate sources/*.yml
uv run pytest                # runs the guardrail suite
make up                      # postgres + minio + dagster (docker compose)
make demo                    # Phase-1 A1 skeleton end-to-end (once bodies are implemented)
```

## Phase-1 "definition of done"

`make up && make demo` runs Dagster end-to-end: **EU TED via Kingfisher → raw →
clean → OCDS + FtM → 3–5 EU-validated indicators → published tier-a aggregates →
dashboard renders them**, with every record provenance-stamped and every guardrail
test green in CI.

## Stack

FollowTheMoney · OpenAleph (spike, kept off the default path) · OpenSanctions
nomenklatura (Phase 2) · OCDS + OCP Kingfisher/Cardinal · Splink (Phase 2) ·
Dagster · Postgres + MinIO · Vite/React dashboard.

## License

Code: **MIT**. **Data is not code** — third-party source data carries its own terms
(tracked per source in `sources/*.yml`); some (e.g. OpenSanctions, CC-BY-NC) are
excluded from bulk open exports. See `LICENSE` and `TRADEMARKS.md`.

---

*Not legal advice. No personal-data processing before a written legal opinion per
jurisdiction and the A2 DPIA.*
