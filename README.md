# Decorruptio

[![CI](https://github.com/Pher217/decorruptio/actions/workflows/ci.yml/badge.svg)](https://github.com/Pher217/decorruptio/actions/workflows/ci.yml) [![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE) [![tests](https://img.shields.io/badge/tests-1101%20passing-brightgreen)](#quickstart) [![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](pyproject.toml) [![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](CONTRIBUTING.md)

> **Follow the world's public money. Surface anomalies for human investigators.
> Publish reproducible transparency data.** Risk indicators for investigation —
> **never verdicts of guilt.**

Most "expose corruption with AI" projects fail the same two ways: they publish
accusations they cannot defend, and they never find out whether their detector
actually detects anything. Decorruptio is built so neither can happen quietly.
The legal guardrails are executable tests, and the measurement boundary is
**fail-closed** — if coverage of the underlying data is unknown, the pipeline
refuses to emit a score and publishes a certificate saying exactly which control
blocked it.

**That certificate currently reads `NO SCORE`.** The honest headline is below.

> The Python package is named `uncorrupt` (the original working name); the project
> and repo are `decorruptio`. Same thing.
>
> **Design docs — the system spec, ADR-000…ADR-009, and the research/null
> write-ups — live in a separate private Obsidian vault, not in this repo.** This
> repo is code, config, and the executable guardrails. Where a docstring cites
> `02 Projects/Ideas/Decorruptio/...`, that is a vault path; the load-bearing
> content is summarised here. If you're contributing and need an ADR, open an
> issue and it gets published.

---

## Status, honestly: a measured null, and a project looking for collaborators

Phase 1 asked one question: **can a detector for procurement bid-rigging be built
and validated on UK open data as published?** The answer we measured is **no**, and
we can say precisely why. This is a null result, not an abandoned project — and it
is the reason this repo is now open.

**The fail-closed gate was built, then run against itself — and it failed:**

| Coverage gate | Accounted for | Standard | Result |
|---|---|---|---|
| Companies House officer roster over the procurement-supplier universe | **63 / 12,227 (0.52%)** | 100% accounted | **BLOCKED** |
| Commons register ingest vs. the Interests API's own `totalResults` | **1,675 / 3,237 (51.7%)** | 100% accounted | **BLOCKED** |

Those two are the coverage half. The checked-in
`experiments/no_score_certificate.json` — a permanent, auditable artifact bound to
a code commit and an attestation-inclusive graph hash
([ADR-008](#the-guardrails-are-executable-not-prose)) — records
`"verdict": "NO SCORE -- INSTRUMENT-LIMITED"` against **five** named blockers: the
two coverage gates above plus three per-stratum control batteries
(`commons_declared_interest`, `lords_declared_interest`, `ch_officer_appointment`).
Every company without a durable cache file counts `not_attempted`, never silently
dropped. **Unknown never reads as pass.**

**The instrument was then tested against a case known to be true** — CMA Case
50697, the demolition cartel (decision 12 June 2023, ~£60m fines, 19 rigged
contracts, five-year conduct period):

- Identity resolution: **10/10** firms resolve to Companies House numbers — **PASS**.
- Coverage: **29 awards matched**, **2 inside the offence window**, and **0 of the
  19 rigged contracts present** — **FAIL**. Only 5 of 20 addressee entities resolve
  to any procurement award at all.
- The absence is not buyer-coverage failure: three of the four public buyers are
  well represented in the corpus.

**Four independent retrospective signals, four nulls — each killed by a control,
not abandoned:** officer overlap died on its own corporate-group-collapse test
(zero concurrent cross-group seats); "rename + debt" was unsupported and
underpowered (brand-breaking rename 26.3% vs 23.3%, p=0.54) and ran *backwards*
(cartelists carry **fewer** charges, 5.3% vs 16.7%, and less insolvency history,
10.5% vs 25.3%); disqualified directors "apparently holding live directorships"
corrected from 50.3% to **1.9% (3 of 157)** once dissolved companies were excluded
— three case studies, not a signal. Negative controls held throughout, so these
are failures to detect a real signal rather than a dead instrument.

**The generalisable claim is a ceiling, not a constant.** Competition offences are
market-wide; procurement transparency observes only the public-buyer slice. Any
procurement-only detector has a recall ceiling equal to the public share of the
conduct — and that share is unknowable ex ante. In our two-case sample it varies
enormously (21.1% public in CMA 50697; substantially public in OFT CE/4327-04).
**We deliberately publish no percentage estimate.**

**What is *not* claimed:** not that bid-rigging detection is impossible (N=1
adjudicated positive); not that the indicators are false — they are **untested at
adequate power (~20–25%)**. The binding constraint is **label supply, not
detection**: roughly 14 usable labels exist against 30–50 needed. And the data that
would let anyone test this prospectively **only began existing in February 2025**
under the Procurement Act 2023 — post-Act Find a Tender names losing bidders *with
Companies House numbers*, seen on 93/94 sampled releases but only **3.2% of award
releases**.

### The methods confession, kept in public on purpose

Across this work, **seven** conclusions of the form "count = 0 / no matches / does
not exist" turned out to be wrong — each one closing a line of work on a
mis-specified probe. The root cause is an asymmetry: a positive result generates
work and gets scrutinised; an absence closes work and gets recorded.

The standing rule adopted, and enforced in review: **any absence that closes a line
of work ships with (a) a positive control — the same probe run against something
known present — and (b) the verbatim command.** The seventh instance proved even
that is insufficient: a passing positive control proves a probe can find *one*
thing, not that an enumeration is *complete* (a PDF line-wrap hid 2 of 20 company
numbers while the control passed). Completeness needs an independent count, or a
control member chosen because it is *hard*.

Every correction in the null moved a number **against** the flattering direction:
17→29 awards but 1→**0** rigged contracts covered; 50.3%→1.9%; 18→20 entities. That
is the evidence the null survived adversarial checking rather than avoiding it.

---

## Where this is stuck — and how you can help

I've taken this as far as one person plus a measurement discipline can. The
blocking problems are **not** "write more code":

1. **Label supply is the binding constraint.** ~14 usable labels against 30–50
   needed. Everything downstream — power, validation, any claim that an indicator
   works — is gated on this. **If you have or can construct adjudicated
   ground-truth relationships (enforcement decisions, court records, audit
   findings, journalism with citable primary sources), that is the single highest-value
   contribution to this repo.** See `tests/fixtures/*_controls.json` for the shape.
2. **Gate 0, unresolved fork.** Either curate `data/gold_manifest.csv` for real, or
   formally retire the coverage-gated pipeline with its own ADR. Everything else is
   downstream of that decision. (`scripts/load_gold_manifest.py`,
   `scripts/run_gold_benchmark.py`, `tests/fixtures/gold_manifest.example.csv`.)
3. **The Commons coverage figure may be a measurement defect, not a data gap.** The
   fetch retrieves all 3,237 records; ~1,562 are dropped in the graph-write step by
   *deliberate* privacy exclusions (`skipped_family`, `skipped_private_individual`),
   which the gate wrongly counts as missing rather than accounted-for-excluded.
   Confirming `1,675 + the four skip counters == 3,237` exactly would clear half of
   Gate 0 cheaply. (`src/uncorrupt/gates/coverage.py::measure_commons_coverage`.)
4. **A known bug with a measured blast radius.** Companies House packs multiple
   companies into a single `company_names` string
   (`"BROWN AND MASON GROUP LIMITED (01892133)  BROWN AND MASON LIMITED (00686405)"`),
   which likely affects the 1.9% disqualified-director figure.
   `scripts/disqualified_director_cross_register.py` needs re-running with that
   accounted for.
5. **Alias matching is load-bearing and under-built.** Cantillon Limited
   (`00916538`) became MORRISROE DEMOLITION LIMITED six weeks after the decision
   disqualifying two of its directors. Current-name matching returns **zero**; alias
   matching recovers it. Sanctioned entities are the ones most likely to have
   rebranded — so this is a false negative on precisely the highest-value rows.
6. **Another jurisdiction, assessed honestly before any build.** Romania was probed
   and came back **weak** (SICAP's live OpenAPI has no bid/tenderer endpoints; DNA
   press releases name companies in prose with zero CUI occurrences — the same
   failure that killed Ukraine). Genuinely unassessed UK surfaces remain: company
   accounts/iXBRL, Land Registry, local-authority spend, the Section 70 register.
   A jurisdiction where **losing bidders are published with company identifiers** is
   the thing to look for.
7. **A prospective monitor may be viable where the retrospective one is not** —
   post-Feb-2025 Find a Tender. Sizing that (high-value tier + trend since Feb 2025,
   not the 3.2% headline rate) is deliberately not part of the null and is open work.

Two entry points are designed for external contribution and gated by CI:
**connectors** (`src/uncorrupt/connectors/`) and **indicators**
(`src/uncorrupt/indicators/`), both discovered via `pyproject.toml` entry points.
See [CONTRIBUTING.md](CONTRIBUTING.md).

---

## The guardrails are executable, not prose

This project deliberately does **not** algorithmically "expose corrupt
politicians" — that is illegal (GDPR/LGPD), defamation-exposed, and unsound
(automated flagging is a triage funnel: the best deployed system turns ~17,700
flags into ~134 actions). So the guardrails live in code and CI, not in a
manifesto:

- **`sources/`** — a machine-readable legal-basis + redistribution register (13
  source entries). A connector **cannot run** without a valid entry.
  `opensanctions.yml` ships marked `non_commercial` + `A2` + `dpia_cleared: false`
  so the guardrail tests have a real failing case to bite on.
- **`tests/guardrails/`** — CI fails if a tier-b/c field reaches a tier-a export, if
  a non-redistributable source leaks into a bulk open export, if a flag lacks
  provenance, or if a raw national ID is serialized.
- **`src/uncorrupt/core/`** — provenance + version stamps, A1/A2/B data classes,
  publication tiers, composite company key, FX-at-date, circuit breaker. Everything
  imports this.
- **`src/uncorrupt/vault/`** — keyed-HMAC tokenizer; refuses to run without a key,
  never returns a raw ID.
- **`src/uncorrupt/gates/`** — the fail-closed measurement boundary (ADR-008):
  `coverage.py`, `stratum.py`, `certificate.py`, `binding.py`. A blocked gate emits
  a **no-score certificate** bound to a code commit + graph hash, rather than a
  score with an asterisk.
- **Indicators are disabled-until-validated** — an indicator only runs in a locale
  explicitly marked `VALIDATED`. An intuitive indicator can be worthless.
- **Identity is asserted, never merged** (ADR-006) — cross-register matches are
  `Attestation`s with confidence, not silent entity fusion.
- **`TRADEMARKS.md`** — the code is MIT and forkable; the guardrails are not
  technically enforceable in a fork, so the project name is a mark a
  guardrail-stripping fork may not use.

## Architecture (6 layers → Django ORM + cron)

```
L1 Connectors (httpx fetch, ingestion only — no parsing)
  → L2 Staging (Django ORM → PostgreSQL: JSONB + hashed local files)
  → L3 Indicators (Indicator ABC, disabled-until-validated)
  → L4 Flags (provenance + version stamp on every flag)
  → L5 Review Workspace (DRF API + Django admin)
  → L6 Publication (tiered: open data export / vetted feed)
```

Scheduled via cron + Django management commands — **no Dagster, no MinIO, no object
store, no Vite/React dashboard** (retired per ADR-005 D6). `dagster.yaml` and
`dashboard/` are still tracked leftovers, pending deletion — do not build on
them. The `Dockerfile` used to `CMD ["dagster", "dev", ...]`, which could never
have started since Dagster is not a dependency; it now serves the Django/DRF app. FtM-*shaped* models live in the
Django ORM, but the project does **not** depend on the FollowTheMoney library,
Aleph, or nomenklatura, and OCP indicators are not used.

### Layers beyond the six

- **`graph/`** — the relationship layer: `Entity` / `Alias` / `Edge` /
  `Attestation`, plus identity resolution, GLEIF resolution, and the benchmark
  runner.
- **`extraction/`** — PDF / HTML / OCR document extraction with a quality gate
  (enforcement decisions are PDFs; the CMA decision in `sources/primary/` is
  content-hashed).
- **`mcp/`** — a read-only MCP server (`server.py`, `tools.py`, `privacy.py`) so an
  agent can query the graph under the same privacy rules as the API.
- **`resolve/` `normalize/` `triage/` `review/` `publish/` `research/` `register/`**
  — resolution, normalisation, triage, human review, tiered publication, research
  runners, and the source register loader.

### Data sources currently ingested

Nine ingest paths write into the graph: **EC donations**, **Commons register of
interests**, **Lords register**, **Companies House officers**, **CH officer
appointments**, **GLEIF LEIs**, **Register of Overseas Entities**, **OpenSanctions**
(persons + a non-personal entity slice), and **register snapshots**. Procurement
connectors: **EU TED**, **UK Contracts Finder**, **Ukraine ProZorro**, **Colombia
SECOP II**, plus **GLEIF**. Locale profiles: `gb`, `eu`, `ua`, `co`, `br`.

### Indicators

`i001` single bidder · `i002` short bid window · `i003` repeat-winner share ·
`i004` price vs estimate · `i005` direct-award share — all registered as entry
points. `i006` incorporation proximity · `i007` value vs company size · `i008`
dormancy/delinquency are implemented and tested, and used directly by
`scripts/kill_experiment.py`, but are **deliberately not registered** in
`pyproject.toml`, so `load_indicators()` and `enabled_for()` never return them.

That is not an oversight to fix casually. All three carry
`"gb": ValidationStatus.VALIDATED` in their class body, so registering them
would make them **live in the `gb` locale** on the strength of a validation
claim that predates the null above and is not defensible at ~20–25% power.
Registering them is therefore a decision about what the project is willing to
assert, not a packaging chore — either the `VALIDATED` marks come down first, or
they stay unregistered. Raise it as an issue rather than opening a one-line PR.

## Scaling split (why the layout looks like this)

- **A1** — non-personal money data (contracts/budgets/companies): scales globally.
  **Phase 1 builds only A1.**
- **A2** — public-persons data (PEPs/office-holders/sanctions/BO): gated by an
  up-front global DPIA; `A2` connectors refuse to load until `dpia_cleared: true`.
- **B** — flagging & investigation: gated per-jurisdiction by a partner + legal
  opinion.

## Quickstart

```bash
uv sync --extra dev                      # install (Python 3.12+)
uv run uncorrupt validate-registry       # load + validate sources/*.yml
uv run ruff check . && uv run ruff format --check .
uv run mypy                              # strict on core/, register/, vault/
uv run pytest                            # 1101 tests, incl. the guardrail suite
```

Postgres is optional for the test suite (tests use the Django test settings).
`make up` starts a local Postgres via `docker compose` if you want to run the
ingesters against a real database; `make migrate` applies migrations. See the
`Makefile` for the full target list.

Live ingest against Companies House needs `COMPANIES_HOUSE_API_KEY`, and the
tokenizer needs `UNCORRUPT_VAULT_HMAC_KEY` — copy `.env.example` to `.env`. **Get
your own keys**; none are distributed with this repo.

Some scripts (`scripts/measure_temporal_lift.py`, `scripts/phase_c_paths.py`,
`scripts/cohort_test_v2.py`) read a local cohort CSV that is **not** in this repo
and will refuse to run without it — see [issues](https://github.com/Pher217/decorruptio/issues)
for the plan to replace it with a published fixture.

## A note on personal data in this repo

The test fixtures under `tests/fixtures/` contain **named real people** — MPs,
company officers, political donors — taken from public statutory registers, because
externally-specified ground truth is the only honest way to test whether the
pipeline recovers a known relationship. Every row is traceable to its published
source, and no fixture asserts wrongdoing by anyone. Personal data reaching the
pipeline is governed by the A2 gate above; **flagging** (layer B) is gated behind a
per-jurisdiction legal opinion and is not enabled. If you believe a fixture row
should not be here, open an issue and it will be removed.

## Stack

Django 5.2 + Django REST Framework · PostgreSQL (JSONB + hashed local files;
**monetary values as integer cents, never floats**) · httpx · cron + Django
management commands · Django admin + DRF for review/publication · MCP server for
read-only agent access. Lint/format: ruff. Types: mypy + django-stubs (strict on
the guardrail-bearing modules). Tests: pytest + pytest-django. Optional extras:
`pdf` (pypdf), `ocr` (pytesseract + pypdfium2).

Splink-style entity resolution is Phase 2.

## What this is not

- an automated accusation machine, or anything that publishes a verdict
- a general-purpose OSINT or entity-resolution platform
- a hosted service — there is no SaaS and no data pipeline you can point at yourself yet
- a claim that any indicator in here works: none is validated at adequate power

## License

Code: **MIT**. **Data is not code** — third-party source data carries its own terms
(tracked per source in `sources/*.yml`); some (e.g. OpenSanctions, CC-BY-NC) are
excluded from bulk open exports. See [LICENSE](LICENSE) and
[TRADEMARKS.md](TRADEMARKS.md).

---

*Not legal advice. No personal-data processing before a written legal opinion per
jurisdiction and the A2 DPIA.*
