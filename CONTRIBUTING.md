# Contributing

Design docs (spec, ADRs, research) live in a separate Obsidian vault, not this
repo. Start with the README and the guardrails summary there.

## Adding a data source (connector)
A country/source is added as: **a connector + a `sources/<id>.yml` register entry
+ a `locales/<code>.yml` profile.** The connector will not load without a valid
register entry. Requirements (enforced by `.github/workflows/connector-gate.yml`):
1. `sources/<id>.yml` present and schema-valid (license, redistribution,
   data_class A1/A2, default tier, freshness SLA, legal basis).
2. Connector passes `uncorrupt.connectors.conformance.check_connector`.
3. `data_class: A2` connectors require `dpia_cleared: true` in the register entry.

## Adding an indicator
Implement `uncorrupt.indicators.base.Indicator`, register it under the
`uncorrupt.indicators` entry-point group, and ship a regression test with fixture
OCDS releases. Indicators are **disabled-until-validated**: they only run in a
locale explicitly marked `VALIDATED` (an intuitive indicator can be worthless —
validate against local ground truth first).

## The guardrail tests are not optional
`tests/guardrails/` encodes ADR-000. CI fails if a private/tier-b field can reach
a tier-a export, if a non-redistributable source leaks into a bulk export, if a
record lacks provenance, or if a raw national ID is serialized.
