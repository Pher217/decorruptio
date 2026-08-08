# Security Policy

Decorruptio fetches public-money and public-persons data, resolves it into a
relationship graph, and gates what leaves the system. Its threat model is
unusual for a data pipeline: the interesting failures are not "an attacker
gets in," they are **a guardrail silently not firing** — restricted data
reaching an open export, an unkeyed token, or a blocked measurement gate
reporting a score anyway. Read this before assuming "security" means the
generic OWASP list.

## Supported versions

Pre-1.0 (`version = "0.0.1"` in `pyproject.toml`). There are no tagged
releases. Only `main` is supported — report against the current `main`.

## Reporting a vulnerability

Open a [private security advisory](https://github.com/Pher217/decorruptio/security/advisories/new)
on this repository. Do not open a public issue for a vulnerability.

This is a single-maintainer project. Response is best-effort — there is no
committed SLA. If you don't hear back, that means the report hasn't been
triaged yet, not that it was dismissed.

## In scope

The classes below matter more here than in a typical repo, because they are
exactly what `sources/`, `tests/guardrails/`, `src/uncorrupt/vault/`, and
`src/uncorrupt/gates/` exist to prevent. A way around any of them is a
security issue, not a feature request — file it privately even if you also
have a passing test that demonstrates it.

- **Guardrail bypass.** Anything that lets a tier-b/c field reach a tier-a
  open export, lets a non-redistributable source (e.g. OpenSanctions,
  `redistribution: non_commercial`) reach a bulk open export, lets a flag
  exist without a `ProvenanceRecord`, or lets a raw national ID get
  serialized. `tests/guardrails/` (`test_tier_export.py`,
  `test_redistribution.py`, `test_provenance.py`, `test_vault.py`) encodes
  these; a way past them that those tests don't catch is in scope.
- **Tokenizer weakness.** `src/uncorrupt/vault/tokenizer.py` is a keyed-HMAC
  tokenizer for national IDs. It must refuse to run without
  `UNCORRUPT_VAULT_HMAC_KEY` set and must never return, store, or log a raw
  ID. Anything that makes a token reversible, brute-forceable without the
  key, or correlatable across `id_type` namespaces is in scope.
- **Fail-open in the measurement gate.** `src/uncorrupt/gates/` (ADR-008)
  exists so "unknown" can never read as "pass" — a blocked gate must emit a
  no-score certificate (`gates/certificate.py`) naming exactly which control
  blocked scoring, never a score. Anything that makes a blocked gate emit a
  score anyway, or makes a no-score certificate claim a freeze-state binding
  it doesn't actually have (`gates/binding.py`'s `GateFreezeState`), is in
  scope.
- **The A2 DPIA gate.** A connector registered with `data_class: A2` must
  refuse to load unless its `sources/*.yml` entry has `dpia_cleared: true`
  (`src/uncorrupt/connectors/registry.py`). A way to load or run an A2
  connector without that flag set is in scope.

## Not a vulnerability — report it anyway

- **A factual error in a published figure** — a coverage percentage, a gate
  measurement, a certificate blocker that misdescribes its own gate.
- **A personal-data concern about a specific fixture row or graph record.**
  The README states plainly: *"No personal-data processing before a written
  legal opinion per jurisdiction and the A2 DPIA."* If a fixture, test file,
  or ingested record contains personal data you believe shouldn't be there,
  say so in a private advisory (or a regular issue if you'd rather it not be
  treated as a security report) naming the row. A request to remove a
  fixture row will be honored.

Use the same private-advisory channel above for either — it's fine to say
up front that it's not a vulnerability.

## Out of scope

- **Fork behavior.** The guardrails are enforced in *this* repository's code
  and CI, not cryptographically. A fork can strip `tests/guardrails/` or the
  DPIA check entirely — see `TRADEMARKS.md` for why that's a naming/social
  problem, not a technical one this policy covers. Report guardrail gaps in
  *this* repo; a fork's own removal of them is not a vulnerability in
  Decorruptio.
- **Absence of validation on the indicators.** Indicators (`i001`–`i008`)
  are risk indicators for investigation, not verdicts, and are not claimed
  to be statistically validated against ground truth outside the described
  Phase-1 benchmark. This is a documented, published limitation of the
  current phase, not a vulnerability.
