# `sources/` — the connector register

One `sources/<source_id>.yml` per data source. **A connector cannot run without a
valid entry here** (ADR-001 D5): `load_source(source_id)` raises `RegisterError`
if the file is missing, and `SourceEntry` (`src/uncorrupt/register/models.py`)
rejects the file if it is malformed. This is what makes adding a country a
**config change** — new `sources/*.yml` + `locales/*.yml` files plus a small
amount of connector code that reads them — rather than a rewrite. If you are an
agent onboarding a new country, this file is the contract; you should not need
to read the rest of the codebase to follow it.

## Two connector families, one register

Every entry has a `connector_kind`, defaulting to `procurement`:

- **`connector_kind: procurement`** — the OCDS/OCP-style spend pipeline
  (`src/uncorrupt/connectors/<source_id>/connector.py`, implementing the
  `Connector` protocol in `src/uncorrupt/connectors/base.py`), driven by
  `EvaluationContext(locale, source_id)` in `src/uncorrupt/indicators/`.
  Registered via the `uncorrupt.connectors` entry-point group in
  `pyproject.toml`; loaded by `src/uncorrupt/connectors/registry.py`, which also
  enforces the A2/DPIA gate (`dpia_cleared`) at load time.
- **`connector_kind: graph`** — the relationship-recovery layer
  (`src/uncorrupt/graph/<module>.py`), one module per source with a
  `fetch_<x>` + `ingest_<x>` pair (no `Connector` protocol, no entry-point
  registration — see "Wiring a graph connector" below). This family existed
  **outside** the register until this pass; every graph module must now resolve
  its own entry via `load_source(SOURCE_ID)` as the first line of every public
  function, exactly like the procurement family.

Both families are validated by the same Pydantic model (`SourceEntry`) and the
same `sources/_schema.json` — a `connector_kind: graph` entry just declares a
few extra fields that a `procurement` entry does not need. `SourceEntry`'s
model validator enforces this (`_graph_connector_declares_its_contract` in
`src/uncorrupt/register/models.py`) — an incomplete graph entry fails to load,
the same way a missing entry does.

## Fields every entry needs

See `sources/_schema.json` for the authoritative list + enum values. Shared by
both families: `source_id`, `name`, `jurisdictions`, `data_class` (`A1`
non-personal / `A2` public-persons, gated by DPIA / `B` flagging), `tier`
(`a` open / `b` vetted / `c` named), `license`, `redistribution` (`open` /
`attribution` / `non_commercial` / `no_redistribution`), `legal_basis`,
`access_method` (`bulk-api` / `open-data-dump` / `scrape` / `ocr`),
`freshness_sla_days`, `dpia_cleared` (default `false`).

**Graph connectors additionally require** (enforced by the model validator, not
just documentation):

| Field | What it is | Example |
|---|---|---|
| `locale` | The `locales/<code>.yml` this connector's data is denominated in. Omit **only** when `jurisdictions == ["GLOBAL"]` (e.g. GLEIF — reference data with no single country). | `gb` |
| `registry_schemes` | The `Entity.registry_scheme` values this connector emits — the naming convention below. | `[GB-COH, EC-REGULATED-ENTITY]` |
| `identifier_field` | What the connector resolves/dedupes entities on. Free text — describe the primary key and any documented fallback. | `"company_number (fallback: exact normalised name)"` |
| `rate_limit` | The documented API rate limit, or the connector's own self-throttle if none is published. | `"600 requests / 5 minutes"` |

## Registry-scheme naming convention

`registry_scheme` on `Entity` identifies **which register a `registry_id` was
issued by** — never guess an identifier's provenance from jurisdiction alone
(see `src/uncorrupt/graph/gleif.py`'s `COMPANIES_HOUSE_RA_CODES` for why: GB
alone does not mean Companies House). Pattern: `{COUNTRY}-{REGISTER}`, upper
case, hyphenated; a `-UNRESOLVED` suffix marks a scoped placeholder for a named
counterparty that did **not** resolve to a real register entry (never a shared
node keyed on a bare name — duplication over merging is the governing
principle across every graph connector). Global (non-country) schemes have no
country prefix.

Schemes in use today:

| Scheme | Connector | Meaning |
|---|---|---|
| `GLEIF-LEI` | `gleif.py` | GLEIF Legal Entity Identifier (global) |
| `GB-COH` | `ec_donations.py`, `lords_interests.py`, `parliament_interests.py`, `ch_officers.py`, `ch_appointments.py` | UK Companies House company number |
| `GB-COH-OFFICER` / `GB-COH-OFFICER-UNRESOLVED` | `ch_officers.py`, `ch_appointments.py` | CH officer ID / scoped placeholder when no stable ID exists |
| `EC-REGULATED-ENTITY` | `ec_donations.py` | Electoral Commission regulated-entity ID (or name, if no ID) |
| `UK-PARLIAMENT-MEMBER` | `lords_interests.py`, `parliament_interests.py` | UK Parliament member ID (shared across Commons and Lords) |
| `UK-LORDS-UNRESOLVED` / `UK-PARLIAMENT-UNRESOLVED` | `lords_interests.py` / `parliament_interests.py` | Scoped placeholder for a named counterparty that did not resolve |

A new country introduces its own prefix (e.g. `BR-CNPJ` for Brazil's Cadastro
Nacional da Pessoa Jurídica — see `locales/br.yml`) rather than reusing an
existing one.

## Onboarding a new country — order of operations

1. **Add `locales/<code>.yml`** (see `locales/_schema.json`): `code`, `currency`,
   `name_normalization_profile` (start with `passthrough` — every locale does,
   Phase 1-wide), and `procedure_metadata` for whatever thresholds you can
   **cite** (statute, decree, official source). Leave a threshold **out of the
   dict entirely** if you cannot cite it — do not set it to a guessed number,
   and do not set it to `null`: `dict.get(key, default)` (how indicators read
   `procedure_metadata`, e.g. `i002_short_bid_window.py`) only falls back to the
   indicator's own default when the key is **absent**, not when its value is
   `null`. Document what's missing in `notes` instead. `locales/br.yml` is the
   worked example of this — real currency and registry facts included, real
   procurement-law thresholds included **with a citation**, bid-window minimums
   left out with a TODO because no citable figure was found.
2. **Add one `sources/<source_id>.yml` per external data source** the country
   needs — a procurement portal, and/or any relationship-recovery register —
   **before** writing connector code. Set `connector_kind` and every field
   above. `uv run uncorrupt validate-registry` (also a CI gate,
   `.github/workflows/connector-gate.yml`) loads every entry and fails on a bad
   one.
3. **Procurement connector**: implement `Connector` in
   `src/uncorrupt/connectors/<source_id>/connector.py` (see
   `src/uncorrupt/connectors/gleif/connector.py` for the minimal shape), set
   `source_id`/`jurisdictions`/`data_class` to match the register entry, and
   register it under `[project.entry-points."uncorrupt.connectors"]` in
   `pyproject.toml`. `tests/connectors/test_conformance.py` +
   `src/uncorrupt/connectors/conformance.py` (`check_connector`) verify the
   register entry resolves and `discover`/`fetch` exist.
4. **Graph/relationship connector**: implement `fetch_<x>` + `ingest_<x>` in a
   new `src/uncorrupt/graph/<module>.py`, mirroring `gleif.py` (simplest),
   `ec_donations.py`, or `lords_interests.py` (most complex — HTML scrape +
   Wayback snapshots). No entry-point registration needed. **The one
   non-negotiable wiring step**: the very first line of every public function
   (every `fetch_*` and `ingest_*`) must be
   `load_source(SOURCE_ID)` — a module-level constant matching the
   `source_id` in your `sources/*.yml`. This is the whole contract: a missing
   or invalid register entry raises `RegisterError` before any network call or
   database write, exactly as it already does for the procurement family. See
   `gleif.py`, `ec_donations.py`, `lords_interests.py` for the pattern, and
   their test files' `Test*RegisterContract` classes for how to test it
   (`monkeypatch.setattr(<module>, "SOURCE_ID", "does_not_exist")`, assert
   `RegisterError`).
5. **Write tests** — GIVEN/WHEN/THEN, one assertion per outcome, exact
   assertions (see `~/.claude/rules/code-quality.md` if you have it, or just
   follow the existing test files in `tests/graph/`). At minimum: the core
   ingest invariants (identifier-based matches never guess; ambiguous names
   create no edge; money in integer cents) and the register-refusal test above.
6. **Run `PYTHONPATH=.:src pytest tests/ -q`** and
   `ruff format . && ruff check .` before committing.

## Known gap (as of this pass)

`ch_officers.py`, `ch_appointments.py`, and `parliament_interests.py` now have
register entries (`sources/uk_companies_house_officers.yml`,
`sources/uk_parliament_interests.yml`) but are **not yet wired** to
`load_source()` — those three files were being edited by other sessions at the
time this contract was added, so wiring them was out of scope. Each entry's
`notes` field says so explicitly. Wiring them is step 4 above, already done for
you as a config decision — just add the `load_source(SOURCE_ID)` calls.

## What this file is not

It is not a substitute for reading the module you are extending, and it is not
a place to invent facts. If you cannot cite a legal threshold, license term, or
rate limit, say so in `notes` rather than guessing — `opensanctions.yml`
(`legal_basis: "TBD — requires the up-front global A2 DPIA/LIA before ingest"`)
and `locales/br.yml` are the existing examples of doing that honestly.
