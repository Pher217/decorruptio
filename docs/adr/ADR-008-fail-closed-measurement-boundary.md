# ADR-008 — One fail-closed measurement boundary, with status propagated to the verdict

> **Reproduced from the private project vault**, where the ADR series
> (ADR-000…ADR-009) is canonical. ADR-008 is published in-repo because the README's
> "how you can help" items and the executable guardrails in `src/uncorrupt/gates/`
> reference it, and a stranger cannot otherwise read the measurement discipline the
> project is built on. Reproduced verbatim apart from stripping vault-internal
> backlinks and inlining the one cross-reference that load-bearing text depended on.
>
> **Status:** Accepted 2026-08-03. **Type:** decision.

## Context

A cross-project mining pass produced ten candidate tooling adoptions (sentinel
response classification, completeness assertions, a citation verifier, circuit
breakers, query clamps, source-effectiveness tracking, OCR extraction,
extraction validation, typed upstream errors, near-duplicate detection). The
founder challenged whether these were genuinely needed or were cargo-culted from
projects with different constraints, and asked for an independent review.

That review returned a sharper framing than the list:

> **"The key architecture decision is smaller than the list suggests: establish
> one fail-closed measurement boundary, then make every path to a published
> verdict pass through it."**

## The finding that reframes everything

> *"The largest omission is **status propagation all the way to the verdict**. A
> fetch layer can correctly identify `PARTIAL`, yet the protection is useless if
> staging turns it into zero rows, the graph turns that into no edge, and the
> scorer turns no edge into `REFUTED`."*

Every silent-failure defect this project has hit is an instance of that: a status
was known somewhere and **discarded before it reached the decision**. The EC
endpoint's ignored pagination, the Commons register at 130 of 4,057, the parser
resolving `peer_type` to `""` for all 789 members — in each case the information
needed to detect the problem existed at one layer and was lost by the next.

## Decision

**Completeness is a non-discardable property**, carried from response → ingest
run → dataset snapshot → case classification → verdict.

Every ingest terminates in exactly one of: **`COMPLETE` · `PARTIAL` · `BLOCKED`
· `FAILED` · `UNVERIFIABLE`**. **Only `COMPLETE` may feed scoring.**

Adopted, in this order:

1. **The fetch/completeness boundary** — sentinel response classification
   (status + headers + body, so a 200 carrying a Cloudflare challenge cannot read
   as data), page-identity detection, advertised-total reconciliation, and typed
   upstream errors.
2. **Document extraction, narrow** — text-layer-first with a quality gate. An
   image-only PDF or a failed text layer must become **`EXTRACTION_UNRELIABLE`,
   never `QUOTE_ABSENT`**. Conflating them would turn an extraction failure into
   apparent evidence of fabrication.
3. **Citation verification, broadened** — a machine-readable verification record
   per manifest row. `NEAR` enters human review; an LLM may explain or prioritise
   it but **must never promote it to benchmark-valid evidence**.
4. **A certified run manifest** — the scorer accepts a typed, hashed
   `BenchmarkRunManifest`, not arbitrary file paths, asserting cohort identity and
   expected case count, required sources and snapshots, completeness and coverage
   states, source-separation constraints, **full per-case classifications rather
   than aggregate counts**, and code/schema/parser/OCR/config versions.
5. **Adversarial controls** — fixtures for HTTP-200-challenge-HTML, repeated
   pages under different pagination parameters, advertised-total mismatch,
   image-only PDF, absent quotation, unresolvable identifier, partial source,
   wrong cohort, all-cases-untestable, and a source-separation violation. **Every
   one must produce `NO SCORE`, not zero findings.**

**Deferred or dropped:** a session circuit breaker rides alongside long-running
ingestion; query clamping is a release gate for the MCP server, not a benchmark
blocker. **Per-source effectiveness tracking and embedding-based near-duplicate
detection are not pre-publication requirements and may never be necessary** —
dropped from the adoption list.

## Also required, and previously missing

- **Cohort identity binding** — hash the selected case IDs; the scorer asserts
  cohort name, count, selection rule and salt. Directly addresses the
  wrong-cohort defect that has recurred here.
- **Coverage gates per case**, not row totals. Overall source completeness does
  not prove *these* subjects have observable officer, interest or temporal
  coverage. Report per case, relationship type, source and award date.
- **Immutable raw evidence** — request parameters, retrieval timestamp, response
  headers, original bytes, hashes, parser versions. *A live URL alone is not
  reproducibility.*
- **Independent control totals** — a source-advertised total is useful but not
  infallible. Add prior-run reconciliation, cursor continuity, key-range checks
  and known-record canaries.
- **A single enforced ingestion path** — *"new checks only help if
  measurement-critical connectors cannot route around them."* This project has
  twice built a framework that the next feature simply bypassed, so the boundary
  must be the only road, not the recommended one.
- **A formal no-score certificate** — a failed readiness check emits an artifact
  naming exactly which controls blocked scoring, making *"we did not score"*
  **auditable rather than discretionary**. (In-repo: `experiments/no_score_certificate.json`,
  force-tracked; produced by `src/uncorrupt/gates/certificate.py`.)

## Consequences

- Scoring is gated on machine-checkable readiness, not on judgement.
- The ten-pattern list collapses into one boundary plus its enforcement — less
  ceremony, less cargo-culting, a smaller surface to get right.
- Work not on the critical path is explicitly deferred rather than carried.
- The honest failure mode becomes **NO SCORE with a reason**, which is a
  publishable outcome.

*Canonical source: vault `02 Projects/Ideas/Decorruptio/06 Decisions/ADR-008-fail-closed-measurement-boundary.md`.*
