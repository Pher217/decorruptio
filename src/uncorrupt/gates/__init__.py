"""Gate-measurement tooling (spec v2.4 §A2.4.2/§A2.4.3, ADR-008).

`scripts/run_gold_benchmark.py` (owned elsewhere, not editable from this
package) SCORES the sealed gold manifest but explicitly does not measure the
`CoverageGate`/`StratumGate` inputs it consumes -- see that script's own
"SCOPE" docstring section. Before this package existed, `experiments/
coverage_gate.json` and `experiments/stratum_gates.json` had no producer at
all, so a run could only ever reach INVALID/INSTRUMENT-LIMITED by
construction (every gate defaults to failing/unavailable when its report is
absent -- see `run_gold_benchmark.load_coverage_gate`/`load_stratum_gates`).

This package is that producer. It is a library of pure, DB-read-only
measurement functions; `scripts/measure_coverage_gate.py` and
`scripts/measure_stratum_gates.py` are the CLIs that call it and write the
JSON contracts `run_gold_benchmark.py` already expects.

Sub-modules:
  `binding`     -- freeze-state (graph hash, code commit, manifest hash) plus
                   an ATTESTATION-INCLUSIVE hash closing a gap in
                   `run_gold_benchmark.compute_graph_hash` (edge-tuples only).
  `coverage`    -- spec A2.4.2 global pipeline-validity coverage: Companies
                   House officer-roster coverage over the procurement-supplier
                   universe, UK Parliament (Commons) register ingest
                   completeness, and Lords frozen-snapshot coverage.
  `stratum`     -- spec A2.4.3 per-material-stratum retrieval/temporal gates:
                   Companies House, Commons, Lords, and (extra, not yet
                   consumed by the scorer -- see the module docstring)
                   Electoral Commission.
  `certificate` -- ADR-008's "formal no-score certificate": a JSON artifact
                   naming exactly which gate blocked scoring and why, emitted
                   whenever a measured gate fails.

Every measurement in this package is FAIL CLOSED (ADR-008): a stratum or
coverage check that cannot be measured (no external control fixture, no
frozen snapshot, no reachable data) is reported `unavailable`/`failing`,
never defaulted to a pass.
"""
