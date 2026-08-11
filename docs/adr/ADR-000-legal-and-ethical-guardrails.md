# ADR-000 — Legal & Ethical Guardrails (Foundational Constraints)

> **Reproduced from the private project vault**, where the ADR series
> (ADR-000…ADR-009) is canonical. ADR-000 and ADR-008 are published in-repo
> (`docs/adr/`) because the README's "how you can help" items and the executable
> guardrails reference them and a stranger cannot otherwise read the discipline
> the project is built on. The remainder of the ADR series stays vault-only for
> now. The project was renamed **CivicLens → Decorruptio** (Python package
> `uncorrupt`); "CivicLens" appears below only where it did in the original
> record. Reproduced verbatim apart from stripping vault-internal backlinks.
>
> **Status:** Accepted 2026-07-21. **Type:** foundational constraint.
>
> *This ADR is not legal advice. Before processing any personal data or
> publishing any finding, obtain a written legal opinion from a data-protection
> lawyer in each target jurisdiction and, ideally, a media-law review of the
> publication model.*

## Context

The platform tracks **public government financial flows** to surface anomalies
and risk indicators of misappropriation, for use by auditors, journalists, and
citizens. The founder's stated ambition includes ML "prediction" of wrongdoing,
resolving natural persons via national ID numbers (CPF/NIF), and publicly
"exposing" politicians.

Those ambitions, taken literally, would make the system **illegal in most
jurisdictions and legally radioactive everywhere else** (data-protection
liability + defamation exposure), and would destroy its credibility with the
very auditors and journalists who are its real users. This ADR sets
non-negotiable constraints that every downstream spec must honor. It is
deliberately written first, before the architecture, because these constraints
*shape* the architecture.

## Decision

### G1 — Follow the money, not the person (data-minimization by design)
The unit of analysis is **the public flow**: budgets, appropriations,
contracts, tenders, payments, grants, and the legal entities that receive them.
Natural persons enter scope **only in their capacity as public officials or as
signatories/beneficial owners of entities receiving public money** — never
private citizens. We do not build dossiers on private individuals.

### G2 — Public officials ≠ private citizens (two-tier data model)
- **Public-role data** (a mayor's votes, declared assets, the contracts their
  office signed, their public asset declarations) is fair game where lawfully
  published, because officials have a reduced expectation of privacy *for their
  public function* and there is a legitimate public interest.
- **Private-life data** (home address, family, health, private financial
  accounts, non-public CPF/NIF-linked records) is **out of scope** regardless of
  whose it is.
The schema must tag every attribute with a tier and **enforce it in code** — the
mechanism is **field-level tier ACLs plus a CI test that a private-tier attribute
cannot reach a tier-a/b export** (not just a policy statement).

> **The line is not where intuition puts it (Fable/CJEU correction).** A private
> company owner who wins public contracts is, under GDPR/LGPD, **still a private
> citizen** — not a public official. The CJEU's *WM & Sovim* ruling (C-37/20,
> 2022) struck down *general public access* to beneficial-ownership registers on
> exactly this privacy ground. Consequences: **(i)** beneficial-owner and other
> private-person data default to **tier-b (access-restricted)**, never tier-a
> open publication; **(ii)** a **DPIA + a per-category legitimate-interest
> assessment (LIA)** is required before any such processing (blocking for a
> person-grade phase); **(iii)** necessity is documented **per attribute** —
> collect the minimum that the public-interest purpose actually requires.

### G3 — National ID numbers (CPF / NIF / etc.): resolve, don't harvest
- Treat national IDs as **sensitive linking keys**, not display data. They are
  used *internally* for deduplication/entity resolution **only when obtained from
  a lawful, official source** (e.g. an official transparency portal that itself
  publishes them for public-money recipients).
- **Never** scrape private ID-lookup services, "consulta CPF" brokers, leaked
  databases, or paywalled people-search APIs. Under Brazil's LGPD the CPF is
  personal data and mass processing without a legal basis is unlawful; under
  GDPR the NIF is personal data and national law (PT/ES) further restricts
  national-ID processing. Sourcing from illegal/leaked data also poisons any
  finding evidentially.
- **Never display or export raw IDs.** Tokenize; show only what the official
  source already made public. **Tokenization is not anonymization:** a bare hash
  of an 11-digit CPF is brute-forceable over a ~10¹¹ keyspace in minutes, and a
  tokenized ID **remains personal data** under LGPD/GDPR either way. Use a
  **keyed HMAC with a managed secret key**, and treat the token as personal data
  requiring a legal basis — not as a privacy get-out.
- **"Lawful source" ≠ "lawful further processing."** Purpose limitation still
  applies: data an official portal publishes for transparency may not be lawfully
  *re-processed* for arbitrary purposes. Note e.g. Brazil's Portal da
  Transparência publishes **masked** CPFs — so full CPFs may not even be lawfully
  obtainable there; do not reconstruct them.

### G3b — Data-subject rights & correction (accuracy is also anti-defamation)
- Provide a **data-subject-rights channel** (access / rectification /
  objection — GDPR Arts. 15–21, LGPD Arts. 17–22) as a specced component, not an
  afterthought.
- A wrong entity-resolution merge is simultaneously **inaccurate personal data**
  (GDPR Art. 5(1)(d)) *and* a defamation vector. The analyst adjudication UI must
  be wired to a **merge-rectification procedure** so corrections propagate to any
  derived flags.

### G4 — "Risk indicator," never "guilty" (no algorithmic verdicts)
- The system outputs **red flags and anomaly scores**, explicitly framed as
  *"warrants review,"* not accusations. Corruption is a legal finding made by
  courts, not by a model.
- Every flag must be **explainable**: which rule/indicator fired, on which
  documents, with what threshold. No black-box "this person is 87% corrupt."
- ML is used for **triage and prioritization** (where should a human auditor look
  first), never for automated judgment. Predictive fraud models have
  well-documented false-positive and fairness problems; treat every output as a
  hypothesis requiring human verification against primary documents.
- **Human-in-the-loop is mandatory** before anything is surfaced as a finding.

### G5 — Publication discipline (defamation & the presumption of innocence)
- Public tiers: **(a)** raw open data + reproducible aggregations (safe to
  publish broadly); **(b)** flagged anomalies (available to vetted
  auditors/journalists under a code of conduct, not auto-published to the open
  web); **(c)** named allegations (only after human investigation, right-of-reply
  to the named party, and legal review — this is journalism, and it follows
  journalistic/legal standards, not an automated pipeline).
- Naming a person as "corrupt" based on a model score is **defamation** in most
  jurisdictions and will get the project and its contributors sued. The
  guardrail is structural: the pipeline **cannot** auto-publish tier (c).

### G6 — Provenance, reproducibility, and audit trail
- Every datum carries **source URL, retrieval timestamp, and a content hash**.
  Every flag links back to the exact source documents.
- The whole pipeline is reproducible: given the same inputs, the same flags.
  This is what makes a finding defensible and is the difference between
  investigative evidence and a rumor.

### G7 — Lawful collection
- Respect `robots.txt`, rate limits, and terms of service; prefer official APIs
  and bulk/open-data downloads over scraping. Many transparency portals offer
  bulk data or APIs specifically for this — use them.
- Keep a per-source **legal basis register**: source, license, legal basis for
  processing, jurisdiction, tier. (In-repo: `sources/*.yml`.)

### G8 — Governance, abuse resistance & the fork problem
- The system is itself a target for **political abuse** (weaponizing flags
  against opponents) and **manipulation** (gaming indicators). Governance:
  transparent methodology, open indicator definitions, an independent
  editorial/ethics review for tier-(b)/(c) escalation, and published correction
  procedures.
- **The fork problem (honest limitation).** This ADR governs **a governed
  instance**, not MIT-licensed **code**. "Structurally impossible to auto-publish
  tier-c" is true of *our* deployment — but anyone can fork the code, strip the
  review gate, and run the "expose politicians" version *under the project's
  reputation*. There is **no code-level defense** against this; open-sourcing the
  backbone is a deliberate trade. Mitigations are social/legal, not technical:
  **(i)** a **name/trademark policy** so a stripped fork cannot call itself the
  project (in-repo: `TRADEMARKS.md`); **(ii)** a **methodology-certification mark**
  granted only to instances that honor these guardrails; **(iii)** stating this
  limitation plainly rather than pretending the code enforces ethics.

## Consequences

- **Positive:** the system is lawful, credible with real users (SAIs /
  journalists / CSOs), evidentially useful, and defensible in court and in public.
  This is exactly how OCCRP, Transparency International chapters, and supreme
  audit institutions actually operate.
- **Cost:** we deliberately give up the "one-click expose a politician" fantasy.
  That capability is not viable — legally or ethically — and pursuing it would
  sink the project. What we build instead is more powerful *because it holds up*.

*Canonical source: vault `02 Projects/Ideas/Decorruptio/06 Decisions/ADR-000-legal-and-ethical-guardrails.md`.*
