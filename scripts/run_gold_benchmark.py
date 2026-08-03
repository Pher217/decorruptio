"""Run the pre-registered Phase C gold-manifest benchmark -- ONE verdict.

Spec (LOCKED, do not deviate): vault
`02 Projects/Ideas/Decorruptio/05 Specs/phase-c-gold-manifest-preregistration.md`,
including AMENDMENT v2 (temporal evidence ladder), v2.1 (unit of analysis +
revised verdict set), v2.3 (manifest schema semantics + the narrowed case
key), v2.4 (control battery, per-stratum gating, freeze protocol), v2.5-v2.6
(snapshot ceiling, evidence-type stratification, office-holding-end-date
clarification), v2.7 (precommitted cohort selection + retrieval_stratum),
v2.8/SEALED COHORT v2 (retrieval strata assigned, the effective denominator --
N_total=20, N_untestable=3 declared BY CONSTRUCTION). Sections 5-7 and every
amendment are binding.

This script does NOT implement a second path-search or resolution stack. It
calls the same code the rest of Phase C already uses and already trusts:

  * `scripts.phase_c_paths.find_paths`       -- the 2-hop path search
  * `scripts.phase_c_paths.resolve_supplier` -- company-side resolution
  * `scripts.phase_c_paths.resolve_referrer` -- person-side (surname) resolution
  * `scripts.phase_c_paths.build_adjacency`  -- the adjacency index
  * `scripts/run_positive_controls.py` / `scripts/run_negative_controls.py`
    -- invoked as subprocesses (never re-implemented), now RECLASSIFIED as
    historical, non-gating diagnostics (see CONTROL BATTERY below).

CONTROL BATTERY (amendment v2.4, A2.4.1/A2.4.2) -- retiring the old single
control set: the graph-derived 30 positive controls and the 200 negative
pairs were quoted throughout this project as if they validated the
instrument. They do not: the 30 are selected FROM relationships already in
the graph, so they test traversal/resolution conditional on the data already
being ingested -- partly tautological, and structurally unable to detect a
missing-ingestion failure (the dominant failure mode: officer coverage is
54/13,129, Commons ~3% ingested). Both are now HISTORICAL DIAGNOSTICS ONLY --
the 30 a regression fixture, the 0/200 a topology snapshot -- and NEITHER
feeds `classify_outcome` nor may appear in the primary results table.

Gating is now split into two INDEPENDENT inputs this script consumes but
does NOT compute (built by a separate control-battery measurement process,
out of this script's scope -- see "SCOPE" below):

  * `CoverageGate` (A2.4.2) -- the two GLOBAL pipeline-validity checks
    (supplier-universe CH officer-roster coverage; Commons universe ingest
    completeness). Either failing means the pipeline itself is broken --
    INVALID, before any stratum is even considered.
  * `StratumGate`, one per MATERIAL STRATUM (A2.4.3) -- externally specified,
    fixed-independently-of-the-graph controls testing retrieval AND
    temporal recovery for exactly that stratum:
      1. Commons `declared_interest`, dated
      2. Lords `declared_interest`, snapshot-dated or atemporal
      3. Companies House officer/appointment paths
    The 10 externally-sourced Commons controls gate ONLY stratum 1; they say
    nothing about Lords or officer-roster completeness. The graph's 5,690
    Lords edges do NOT make Lords validated -- graph abundance is not source
    coverage (A2.4.3). "Lords source coverage" is explicitly "gating --
    currently unavailable" (A2.4.2): until an external Lords control exists,
    `stratum_gates["lords_declared_interest"]` defaults to `available=False`
    and can never pass.

SCOPE -- what this script does NOT implement: the actual MEASUREMENT behind
`CoverageGate`/`StratumGate` (comparing frozen source snapshots against what
the graph actually ingested), the freeze protocol (A2.4.5 -- sealing graph
hashes, source-snapshot dates, control/negative/gold-manifest hashes across
graph versions), and the 2x2 ablation (A2.4.6 -- base / +officer-expansion /
+Commons-fix / +both, run on controls only, gold spent once on the final
state). These are project-level measurement and process disciplines, not
benchmark-SCORING logic -- they belong in their own tooling. This script only
requires their *results* as explicit, safely-defaulting-to-failing inputs
(`load_coverage_gate`, `load_stratum_gates`), exactly as it already does for
the manifest itself.

UNIT OF ANALYSIS (amendment v2.1 A2.1.1, NARROWED by v2.3 A2.3.2): every
threshold below is scored at CASE level, not row level, and a case is the
distinct AWARDEE `company_number` alone -- not `(company_number, award_date)`
as v2.1 first set it. `evaluate_case` rolls a case's constituent rows up into
one status (recovered beats undated_only beats not_recovered beats
untestable beats no_trace_by_design) using the case's EARLIEST qualifying
award date as the cutoff for every row.

`company_number` always means the AWARDEE (spec A2.3.1) -- the loader
(`scripts/load_gold_manifest.py`) rejects any row that does not explicitly
confirm this, so this script can assume it throughout.

Five per-row / per-case outcomes that MUST NOT be conflated -- Phase C v1
collapsed (c) into a refutation and had to retract "0 of 52" because of it:

  (a) recovered           -- a path was found and every edge on it dates
                             before the case's earliest award_date (H1's
                             actual claim). Attributed to the material
                             stratum(s) its evidence belongs to
                             (`classify_edge_stratum`/`path_strata`).
  (b) undated_only        -- a path was found but at least one edge on it
                             has no valid_from, so pre-award is UNDECIDABLE,
                             not false.
  (c) untestable          -- the supplier or the referrer never resolved to
                             a graph entity. Excluded from every denominator;
                             reported separately with the specific reason.
  (d) no_trace_by_design  -- spec A2.3.3: the row's only evidence is
                             Persons-with-Significant-Control data. Expected
                             non-recovery, never a refutation.
  (e) recovered_circular  -- spec SS3: recovery PROVEN to rest solely on a
                             source the row's own `excluded_from_retrieval`
                             names. Excluded from `cases_recovered`.
  (f) recovered_unverifiable_provenance -- adversarial-review Severity 1
                             finding 2: recovery rests only on unattested
                             evidence -- nothing PROVES it circular, but
                             nothing POSITIVELY VERIFIES it as permitted
                             either. Publication-grade evidence fails
                             CLOSED: also excluded from `cases_recovered`,
                             reported separately, never silently folded
                             into "clean".

Only a resolved, non-PSC-only, non-circular, positively-verified pair with
no path at all, dated or undated, is a genuine `not_recovered` miss.

Qualification for (a)/(e)/(f) is decided PER PATH (`PathEvidence`,
`classify_recovered_cases`) -- never by unioning taint or strata across a
case's several found paths (adversarial-review Severity 1 finding 3). A
single path spanning both a passing and a non-passing material stratum does
NOT qualify merely because one of its strata passes, and a clean-but-
unsupported-stratum path can never be paired with a circular-but-passing-
stratum path from a DIFFERENT path on the same case to manufacture a
qualification neither path earns on its own.

VERDICT SET (amendments v2.1 A2.1.2, v2.4 A2.4.4, v2.8 A2.8.5):

  INSUFFICIENT-COHORT  the manifest's case set is not exactly the sealed
                       `PREREGISTERED_COHORT_SIZE`-case cohort, OR the
                       testable count falls below what the sealed cohort's
                       OWN precommitted `PREREGISTERED_UNTESTABLE_BY_CONSTRUCTION`
                       subset allows (v2.8/SEALED COHORT v2: N_total=20,
                       3 declared untestable BY CONSTRUCTION -- this does
                       NOT block scoring; a degenerate testable count from a
                       resolver regression or an outsized no_trace_by_design
                       fraction does). Checked first.
  INVALID              the CoverageGate fails -- pipeline broken.
  INSTRUMENT-LIMITED   no material stratum passes at all, OR (when 0 cases
                       qualify) at least one material stratum is still
                       unvalidated -- the UK strict hypothesis, or that
                       part of it, is untestable with these sources.
  CONFIRMED / PARTIAL  >=4 (CONFIRMED) or 1-3 (PARTIAL) cases recovered
                       through a PASSING stratum's evidence -- source-
                       qualified: the verdict names which strata the
                       recovered paths belong to (see `classify_recovered_cases`
                       and `main()`'s reporting). A case recovered ONLY
                       through an unsupported stratum (e.g. Lords-only,
                       while Lords remains unavailable) does not count here;
                       it is reported separately, per case. NOT gated on
                       benchmark precision (adversarial-review Severity 2
                       finding 7 -- precision is built from a diagnostic
                       amendment v2.4 A2.4.1 retired to non-gating; gating
                       CONFIRMED on it contradicted that retirement).
  REFUTED              0 cases recovered AND EVERY material stratum passes
                       its own retrieval and temporal controls (A2.4.4) --
                       the strongest, least available verdict. A passing
                       Commons gate never rescues Lords, and an unvalidated
                       Lords gate never erases a genuine, independently
                       verified Commons recovery (A2.4.4) -- this is why
                       CONFIRMED/PARTIAL use per-stratum qualification while
                       REFUTED requires the whole battery.
  UNDEFINED-OUTCOME    any input combination the verdict table above does
                       not partition (e.g. a negative `cases_recovered` from
                       a caller defect) -- adversarial-review Severity 2
                       finding 6: a loud, honest refusal to classify rather
                       than a silent PARTIAL fall-through.

COUNTRY_SWITCH IS NOT A VERDICT -- it is an action triggered by PARTIAL,
REFUTED, or INSTRUMENT-LIMITED (never by INSUFFICIENT-COHORT, INVALID, or
UNDEFINED-OUTCOME). See `country_switch_triggered`.

Statistical reporting (amendment A2.5): a `0/N` control or negative-control
count is never printed bare -- `wilson_upper_bound` computes the ~95%
one-sided upper bound reported alongside it, including alongside
`false_positive_rate` itself. Benchmark precision (on the constructed
20-ish-case : 200-negative sample -- itself a non-gating diagnostic, A2.4.1)
is reported labelled as such, never as an operational/field precision
figure, alongside sensitivity (denominator explicitly named as ALL cases)
and false-positive rate as separate, explicitly named metrics.

Usage:
    PYTHONPATH=.:src python scripts/run_gold_benchmark.py \\
        --manifest data/gold_manifest.csv \\
        --coverage-gate-report experiments/coverage_gate.json \\
        --stratum-gates-report experiments/stratum_gates.json \\
        --out experiments/gold_benchmark.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from scripts.load_gold_manifest import (  # noqa: E402
    GoldCase,
    GoldRow,
    ManifestLoadResult,
    load_gold_manifest,
)
from scripts.phase_c_paths import (  # noqa: E402
    build_adjacency,
    find_paths,
    resolve_referrer,
    resolve_supplier,
    surname,
)

from uncorrupt.graph.models import Edge, Entity  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent

# Spec SS6 / A2.1.2 LOCKED thresholds.
CONTROLS_PASS_FRACTION = 0.9  # >=9/10 per material stratum (A2.4.3)
CONFIRM_MIN_CASES = 4  # >=4/20 cases (not rows -- A2.1.1)
CONFIRM_MIN_PRECISION = 0.80  # >=80% benchmark precision

# The "4/20" and "0/20" thresholds are calibrated to a 20-case cohort (spec
# SS5). Applying them to a smaller -- or smaller-than-it-looks -- cohort
# misrepresents statistical power. See PREREGISTERED_COHORT_SIZE below.
PREREGISTERED_COHORT_SIZE = 20

# SEALED COHORT v2 / amendment v2.8 §A2.8.5: of the sealed 20, exactly 3 are
# declared untestable BY CONSTRUCTION (no ingested register could ever carry
# their evidence -- a personal tie, NHS-accounts-only disclosure, a
# journalism-only advisory role) -- precommitted BEFORE any retrieval result
# was viewed, not discovered afterwards. Scoring the sealed cohort must not
# demand these 3 "come back testable" -- that would contradict v2.8's whole
# point (report `0/N_CH`, never `0/20`). The guard below therefore checks
# COHORT MEMBERSHIP (are these the sealed 20 cases at all -- exactly
# `PREREGISTERED_COHORT_SIZE` of them) separately from a DEGENERATE testable
# count (protecting against a resolver regression or an unexpectedly large
# no_trace_by_design fraction wiping out the denominator) -- never a demand
# that all 20 be independently recoverable.
PREREGISTERED_UNTESTABLE_BY_CONSTRUCTION = 3

# Spec A2.4.5 freeze protocol: the 2-hop search budget is FROZEN, not a free
# CLI knob -- Severity 3 finding 9. Widening it after seeing a result is
# exactly the "adding hops" forbidden by spec SS6/A2.6.
LOCKED_MAX_HOPS = 2

# SEALED COHORT v2 (spec, "SEALED COHORT v2 -- 2026-08-03 -- recomputed after
# verification removals (same salt)"): the awardee `company_number`s that ARE
# the sealed 20, published so the selection is independently reproducible
# from the manifest under the fixed salt `decorruptio-gold-cohort-v1:`
# (v2.7 §A2.7.1). `11014884` replaced `09223972` after verification removed
# it from the pool (v2.6/SEALED COHORT v2). Enforced here (Severity 3 finding
# 9) so the runner cannot silently score the 24-case pool, an earlier 20, or
# any other subset as if it were the locked cohort.
SEALED_COHORT_V2_COMPANY_NUMBERS = frozenset(
    {
        "05437166",
        "03456018",
        "01428210",
        "09618361",
        "NI015738",
        "08205551",
        "12597000",
        "SC149147",
        "04398739",
        "10268228",
        "SC179860",
        "03655958",
        "08126173",
        "04757301",
        "00502663",
        "08001168",
        "10603870",
        "NI622060",
        "07042994",
        "11014884",
    }
)

# Material strata (spec A2.4.3) -- the minimum set the sealed benchmark
# defines, independent of whatever the current ingest happens to contain.
STRATUM_COMMONS = "commons_declared_interest"
STRATUM_LORDS = "lords_declared_interest"
STRATUM_CH_OFFICER = "ch_officer_appointment"
MATERIAL_STRATA = (STRATUM_COMMONS, STRATUM_LORDS, STRATUM_CH_OFFICER)

# Commons and Lords `declared_interest` edges share the SAME entity
# registry_scheme (UK-PARLIAMENT-MEMBER covers both Houses -- Parliament's
# member-ID scheme does not distinguish chambers), so the only mechanical way
# to tell them apart is which register ATTESTED the edge.
COMMONS_SOURCE_NAME = "UK Parliament Register of Interests"
LORDS_SOURCE_NAME = "UK House of Lords Register of Interests"

# Priority order for rolling per-row statuses up to a case (higher wins).
# no_trace_by_design (spec A2.3.3) ranks lowest: it is an EXPECTED non-result
# on a PSC-only row, so any other row in the case -- even a mere untestable
# resolution gap -- is more informative and wins the roll-up.
_STATUS_PRIORITY = {
    "recovered": 4,
    "undated_only": 3,
    "not_recovered": 2,
    "untestable": 1,
    "no_trace_by_design": 0,
}

_Z_95 = 1.959963984540054  # two-sided 95% normal quantile


def classify_edge_stratum(edge: Edge) -> str | None:
    """Map one path edge to a spec A2.4.3 material stratum, or None.

    `same_as` identity bridges and non-material edge types (donation,
    ownership, referred_to_lane, associate_of, supplier_of) return None --
    they carry no stratum and are excluded from source-qualification.

    KNOWN LIMIT: if an edge somehow carries attestations from BOTH the
    Commons and Lords source names (a data anomaly -- the two registers are
    disjoint by construction), this returns "commons_declared_interest" and
    silently under-reports the Lords attribution. Flagged, not fixed, since
    it should not occur given how the registers are ingested.
    """
    if edge.edge_type == "officer_of":
        return STRATUM_CH_OFFICER
    if edge.edge_type == "declared_interest":
        source_names = {a.source_name for a in edge.attestations.all()}
        if COMMONS_SOURCE_NAME in source_names:
            return STRATUM_COMMONS
        if LORDS_SOURCE_NAME in source_names:
            return STRATUM_LORDS
    return None


def path_strata(path: list[Edge]) -> frozenset[str]:
    """The set of material strata a single path's evidence touches."""
    return frozenset(s for e in path if (s := classify_edge_stratum(e)) is not None)


@dataclass(frozen=True)
class PathEvidence:
    """The evidence carried by ONE individual found path, kept SEPARATE from
    every other path (spec A2.4.4, adversarial-review Severity 1 finding 3).

    Both `RowEvaluation.path_evidences` and `CaseEvaluation.path_evidences`
    are flat tuples of these -- never unioned or merged into a single
    aggregate taint/strata pair. The defect this exists to prevent: unioning
    across paths lets a single path that touches BOTH a passing and a
    non-passing stratum qualify because the union merely INTERSECTS the
    passing set, and lets a case combine a clean-but-unsupported-stratum path
    with a circular-but-passing-stratum path into something that reads as
    globally clean AND globally passing-stratum-touching, even though no
    single path is both. Qualification (`passes_stratum_gates`) is therefore
    always evaluated per evidence entry, never on a merged view.
    """

    taint: str  # "clean" | "tainted" | "unverifiable" -- spec SS3, this path alone
    strata: frozenset[str]

    @property
    def is_positively_verified(self) -> bool:
        """Spec SS3 / Severity 1 finding 2: only a path proven 'clean' -- not
        merely 'not proven tainted' -- counts as positively verified
        permitted provenance. 'unverifiable' (an unattested edge) and
        'tainted' (a proven excluded-source match) both fail this."""
        return self.taint == "clean"

    def passes_stratum_gates(self, passing_strata: frozenset[str]) -> bool:
        """Spec A2.4.4: THIS path qualifies only if it is positively
        verified, touches at least one material stratum, and EVERY stratum
        it touches is currently passing -- a subset check, not an
        intersection. A path that also touches an unvalidated stratum must
        never qualify merely because it ALSO touches a validated one."""
        return self.is_positively_verified and bool(self.strata) and self.strata <= passing_strata


@dataclass
class RowEvaluation:
    case_id: str
    status: str  # recovered | undated_only | not_recovered | untestable | no_trace_by_design
    reason: str | None = None
    source_separation: str = "not_applicable"
    example_path: list[str] = field(default_factory=list)
    path_evidences: tuple[PathEvidence, ...] = ()

    @property
    def strata(self) -> frozenset[str]:
        """Display-only union of every path's strata (spec A2.4.3 reporting).
        NEVER used for qualification -- see `PathEvidence.passes_stratum_gates`
        and `classify_recovered_cases`, which read `path_evidences` directly."""
        return frozenset().union(*(pe.strata for pe in self.path_evidences))


@dataclass
class CaseEvaluation:
    """The case-level roll-up of one `GoldCase`'s constituent rows (A2.1.1, A2.3.2)."""

    case_key: str
    company_number: str
    row_count: int
    award_count: int
    earliest_award_date: str
    row_case_ids: list[str]
    status: str  # recovered | undated_only | not_recovered | untestable | no_trace_by_design
    source_separation: str
    row_evaluations: list[RowEvaluation]
    path_evidences: tuple[PathEvidence, ...] = ()

    @property
    def strata(self) -> frozenset[str]:
        """Display-only union across every contributing path -- see
        `RowEvaluation.strata`'s docstring; never used for qualification."""
        return frozenset().union(*(pe.strata for pe in self.path_evidences))

    @property
    def is_concentrated(self) -> bool:
        return self.row_count > 1


@dataclass(frozen=True)
class CoverageGate:
    """Spec A2.4.2 global pipeline-validity coverage controls: supplier-
    universe CH officer-roster coverage, Commons universe ingest
    completeness. Either failing means the pipeline itself is broken --
    INVALID, before any stratum is even considered.

    Like `StratumGate`, pass/fail is a PROPERTY recomputed from the
    underlying counts every time, never a stored/trusted flag -- a producer
    cannot force a pass without the numbers to back it.

    NOTE: spec A2.4.2 describes these as testing whether coverage is
    "complete"/"received complete officer rosters" without stating a
    numeric bar. This implementation uses the same >=90% threshold as
    everything else in the battery (`CONTROLS_PASS_FRACTION`) as the most
    defensible default pending an explicit definition from whoever builds
    the actual measurement -- flagged here rather than silently guessed at
    elsewhere.
    """

    covered: int | None = None
    total: int | None = None
    commons_covered: int | None = None
    commons_total: int | None = None

    @property
    def supplier_universe_passed(self) -> bool:
        # Severity 3 finding 8: `covered` must be a genuine subset of `total`
        # -- without the `covered <= total` bound, a malformed report (e.g.
        # 2/1) would compute a ratio > 1.0 and pass regardless of
        # `CONTROLS_PASS_FRACTION`.
        return (
            self.total is not None
            and self.total > 0
            and self.covered is not None
            and 0 <= self.covered <= self.total
            and (self.covered / self.total) >= CONTROLS_PASS_FRACTION
        )

    @property
    def commons_universe_passed(self) -> bool:
        return (
            self.commons_total is not None
            and self.commons_total > 0
            and self.commons_covered is not None
            and 0 <= self.commons_covered <= self.commons_total
            and (self.commons_covered / self.commons_total) >= CONTROLS_PASS_FRACTION
        )

    @property
    def passed(self) -> bool:
        return self.supplier_universe_passed and self.commons_universe_passed


@dataclass(frozen=True)
class GateBinding:
    """Spec A2.4.5: a frozen coverage/stratum gate authorizes scoring only
    for the EXACT graph + code + manifest state it was measured against --
    never a floating trust token (Severity 3 finding 10). The caller (`main`)
    supplies the CURRENT state; `load_coverage_gate`/`load_stratum_gates`
    refuse (fail closed to the all-unavailable default) whenever the gate
    JSON's own recorded state does not match -- a stale gate can never
    authorize scoring against a different graph.

    `binding=None` (the default) skips verification entirely -- used by
    tests that only care about the retrieval/temporal-count logic and supply
    no binding-relevant fields at all. `main()` always supplies a real one.
    """

    code_commit: str
    graph_hash: str
    manifest_hash: str

    def matches(self, data: dict) -> bool:
        return (
            data.get("code_commit") == self.code_commit
            and data.get("graph_hash") == self.graph_hash
            and data.get("manifest_hash") == self.manifest_hash
        )


def compute_graph_hash() -> str:
    """Canonical, order-independent graph hash (spec A2.4.5) -- a drift
    detector, not a cryptographic commitment: sha256 over every edge's
    (edge_type, source_entity_id, target_entity_id, valid_from) tuple,
    sorted so insertion order never changes the hash."""
    rows = sorted(
        Edge.objects.values_list("edge_type", "source_entity_id", "target_entity_id", "valid_from")
    )
    h = hashlib.sha256()
    for row in rows:
        h.update("|".join(str(x) for x in row).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()


def current_code_commit() -> str:
    """Spec A2.4.5's "code commit" binding field. Falls back to the literal
    string "unknown" rather than raising -- a missing git checkout must not
    crash a benchmark run -- but "unknown" can never match a real recorded
    commit hash, so binding verification still fails closed."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def compute_manifest_hash(path: Path) -> str:
    """Spec A2.4.5's "gold-manifest hash" binding field."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_coverage_gate(path: Path, binding: GateBinding | None = None) -> CoverageGate:
    """Load the spec A2.4.2 global coverage gate result, if measured.

    Expected JSON contract, produced by a separate coverage-measurement
    process (not implemented here):

        {
          "supplier_universe_covered": int, "supplier_universe_total": int,
          "commons_universe_covered": int, "commons_universe_total": int,
          "code_commit": str, "graph_hash": str, "manifest_hash": str
        }

    Returns the all-False default (`CoverageGate()`) if the file does not
    exist -- a missing measurement is never an implicit pass. Likewise if
    `binding` is supplied and the report's own `code_commit`/`graph_hash`/
    `manifest_hash` do not match it (Severity 3 finding 10) -- a gate
    measured against a different graph, code version, or manifest can never
    silently authorize the CURRENT run.
    """
    if not path.exists():
        return CoverageGate()
    data = json.loads(path.read_text())
    if binding is not None and not binding.matches(data):
        return CoverageGate()
    return CoverageGate(
        covered=data.get("supplier_universe_covered"),
        total=data.get("supplier_universe_total"),
        commons_covered=data.get("commons_universe_covered"),
        commons_total=data.get("commons_universe_total"),
    )


@dataclass(frozen=True)
class StratumGate:
    """One material stratum's gate (spec A2.4.3/A2.4.4).

    Built by a separate, externally-specified control-battery measurement
    out of this script's scope. `retrieval_passed`/`temporal_passed` are
    PROPERTIES recomputed from the underlying counts every time -- there is
    no stored boolean to trust or distrust, closing the same "claimed passed
    without the numbers to back it" gap the single-gate design (retired)
    needed a special fix for.

    `available=False` (the default) means no external gating control exists
    for this stratum AT ALL -- the mechanism spec A2.4.2 demands for "Lords
    source coverage -- gating -- currently unavailable": omit the entry (or
    set available: false) and it can never pass, so REFUTED (which requires
    every stratum to pass) can never fire while Lords lacks one.
    """

    available: bool = False
    retrieval_recovered: int | None = None
    retrieval_total: int | None = None
    temporal_recovered: int | None = None
    temporal_total: int | None = None

    @property
    def retrieval_passed(self) -> bool:
        # Severity 3 finding 8: same 0 <= recovered <= total bound as
        # CoverageGate -- a malformed 2/1-style report must not compute a
        # >1.0 ratio and pass.
        return (
            self.retrieval_total is not None
            and self.retrieval_total > 0
            and self.retrieval_recovered is not None
            and 0 <= self.retrieval_recovered <= self.retrieval_total
            and (self.retrieval_recovered / self.retrieval_total) >= CONTROLS_PASS_FRACTION
        )

    @property
    def temporal_passed(self) -> bool:
        return (
            self.temporal_total is not None
            and self.temporal_total > 0
            and self.temporal_recovered is not None
            and 0 <= self.temporal_recovered <= self.temporal_total
            and (self.temporal_recovered / self.temporal_total) >= CONTROLS_PASS_FRACTION
        )

    @property
    def passed(self) -> bool:
        return self.available and self.retrieval_passed and self.temporal_passed


def _parse_json_bool(value: object) -> bool:
    """Coerce a JSON gate field to a real boolean (Severity 3 finding 8).

    A well-formed JSON `false` literal already parses to Python's `False`,
    so bare `bool(...)` works for it. But a malformed report that writes the
    QUOTED STRING `"false"` instead of the JSON literal `false` would
    otherwise become truthy through bare `bool(...)` -- any non-empty string
    is truthy in Python, so `bool("false") is True`. Reject that silently
    wrong report instead: a string value is True only if it spells a
    recognised affirmative token.
    """
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def load_stratum_gates(path: Path, binding: GateBinding | None = None) -> dict[str, StratumGate]:
    """Load the spec A2.4.3 per-material-stratum gate results.

    Expected JSON contract -- one entry per material stratum:

        {
          "code_commit": str, "graph_hash": str, "manifest_hash": str,
          "commons_declared_interest": {
            "available": true,
            "retrieval_recovered": 9, "retrieval_total": 10,
            "temporal_recovered": 9, "temporal_total": 10
          },
          "lords_declared_interest": {"available": false},
          "ch_officer_appointment": {...}
        }

    Every entry in `MATERIAL_STRATA` is always present in the returned dict
    -- a stratum missing from the file (or the file itself missing) defaults
    to `StratumGate()` (available=False), never silently omitted from the
    all-strata-must-pass check REFUTED depends on. Likewise if `binding` is
    supplied and the report's own `code_commit`/`graph_hash`/`manifest_hash`
    do not match it (Severity 3 finding 10): every stratum defaults to
    unavailable, exactly as if the file were missing.
    """
    gates = {name: StratumGate() for name in MATERIAL_STRATA}
    if not path.exists():
        return gates
    data = json.loads(path.read_text())
    if binding is not None and not binding.matches(data):
        return gates
    for name in MATERIAL_STRATA:
        entry = data.get(name)
        if not entry:
            continue
        gates[name] = StratumGate(
            available=_parse_json_bool(entry.get("available", False)),
            retrieval_recovered=entry.get("retrieval_recovered"),
            retrieval_total=entry.get("retrieval_total"),
            temporal_recovered=entry.get("temporal_recovered"),
            temporal_total=entry.get("temporal_total"),
        )
    return gates


def validate_locked_protocol(manifest_result: ManifestLoadResult, max_hops: int) -> list[str]:
    """Spec A2.4.5 freeze protocol: the runner must refuse to silently score
    an un-sealed manifest or an unlocked hop budget (Severity 3 finding 9).

    Pure and DB-free -- takes an already-loaded `ManifestLoadResult`, so it
    is unit-testable without a manifest that resolves against real graph
    data. Returns a list of violation messages; an empty list means the run
    matches the locked protocol.
    """
    violations: list[str] = []
    if max_hops != LOCKED_MAX_HOPS:
        violations.append(
            f"--max-hops={max_hops} does not match the locked two-hop setting "
            f"({LOCKED_MAX_HOPS}, spec A2.4.5) -- the sealed benchmark may only be scored at "
            "the frozen hop budget; widening it after seeing a result is forbidden (SS6/A2.6)."
        )
    actual = frozenset(c.company_number for c in manifest_result.cases)
    if actual != SEALED_COHORT_V2_COMPANY_NUMBERS:
        missing = sorted(SEALED_COHORT_V2_COMPANY_NUMBERS - actual)
        extra = sorted(actual - SEALED_COHORT_V2_COMPANY_NUMBERS)
        detail = []
        if missing:
            detail.append(f"missing from manifest: {missing}")
        if extra:
            detail.append(f"not part of the sealed cohort: {extra}")
        violations.append(
            "manifest case set does not match SEALED COHORT v2 (spec A2.7.1) -- "
            + "; ".join(detail)
        )
    return violations


def _resolve_referrer_entities(
    row: GoldRow, people_by_surname: dict[str, list[Entity]]
) -> list[Entity]:
    """Registry ID first when the manifest supplies one; surname ONLY when it
    does not.

    ADVERSARIAL FIX (Severity 1, finding 1): this used to fall through to
    crude surname matching (`resolve_referrer`) whenever a supplied
    `person_registry_id` failed to resolve. Surname matching is a deliberate
    over-match (see `phase_c_paths.surname`'s own docstring: "a hit found
    this way is a candidate ... not a claim about any individual"). Falling
    back to it after a row has ASSERTED a specific registry identity means an
    unrelated namesake's path becomes the named subject's "recovery" -- a
    false accusation against a real person, and exactly the failure mode a
    registry-ID assertion exists to rule out.

    A row that names a `person_registry_id` and gets no match for it is
    therefore `untestable` (empty list here becomes `untestable` via the
    caller's existing "referrer did not resolve" branch) -- NEVER silently
    downgraded to a same-surname candidate set. Surname matching is used only
    for rows that never asserted a registry identifier in the first place.
    """
    if row.person_registry_id:
        return list(Entity.objects.filter(entity_type="person", registry_id=row.person_registry_id))
    return resolve_referrer(row.person_name, people_by_surname)


def _path_taint(path: list[Edge], lowered_excluded: list[str]) -> str:
    """Classify one path as 'clean', 'tainted', or 'unverifiable'.

    'tainted' fires the moment any edge on the path carries an attestation
    whose source_name matches an excluded string (case-insensitive substring,
    either direction). 'unverifiable' means no tainted match was found, but
    at least one edge on the path has zero attestations -- its provenance is
    simply not recorded, so the absence of a match is not proof of
    separation.
    """
    saw_unattested = False
    for edge in path:
        atts = list(edge.attestations.all())
        if not atts:
            saw_unattested = True
            continue
        for att in atts:
            sn = (att.source_name or "").lower()
            if any(exc in sn or sn in exc for exc in lowered_excluded):
                return "tainted"
    return "unverifiable" if saw_unattested else "clean"


def check_source_separation(paths: list[list[Edge]], excluded_sources: tuple[str, ...]) -> str:
    """Best-effort spec SS3 check: did an excluded source make this recoverable?

    Judged 'ok' if AT LEAST ONE found path is 'clean' (no excluded-source
    attestation, no unattested gap) -- an independent, permitted path exists.
    If none are clean but at least one is 'unverifiable', the result is
    'cannot_verify'. Only when every path is positively 'tainted' is the
    result 'violation'.

    KNOWN LIMITS (reported, not hidden):
      * Can only see what was recorded as an `Attestation.source_name`. It
        cannot detect an excluded source that shaped entity resolution or
        ingestion without ever being logged as an attestation.
      * Matching is a free-text, case-insensitive substring test -- only as
        good as how closely `excluded_from_retrieval` wording matches this
        project's own `source_name` conventions.
    """
    if not excluded_sources or not paths:
        return "not_applicable"
    lowered = [s.lower() for s in excluded_sources if s.strip()]
    if not lowered:
        return "not_applicable"
    taints = [_path_taint(p, lowered) for p in paths]
    if "clean" in taints:
        return "ok"
    if "unverifiable" in taints:
        return "cannot_verify"
    return "violation"


def _resolve_pair(
    row: GoldRow,
    adj: dict[int, list[Edge]],
    people_by_surname: dict[str, list[Entity]],
    ch_cache: dict,
    max_hops: int,
    cutoff: date,
) -> RowEvaluation:
    """Core recovered/undated_only/not_recovered/untestable decision for one
    row, at an explicit `cutoff`.

    Shared by `evaluate_row` (the row's own `award_date`) and `evaluate_case`
    (the case's `earliest_award_date`, spec A2.3.2) so the resolve + path
    search + source-separation + stratum-attribution logic exists in exactly
    one place.
    """
    supplier = resolve_supplier(row.company_name, ch_cache, row.company_number)
    referrers = _resolve_referrer_entities(row, people_by_surname)

    if not supplier or not referrers:
        missing = []
        if not supplier:
            missing.append("supplier")
        if not referrers:
            missing.append("referrer")
        return RowEvaluation(
            case_id=row.case_id,
            status="untestable",
            reason=f"unresolved: {', '.join(missing)}",
        )

    pre_award, undated = find_paths(
        {r.id for r in referrers}, supplier.id, adj, max_hops, cutoff=cutoff
    )

    lowered_excluded = [s.lower() for s in row.excluded_from_retrieval if s.strip()]

    def _evidence(paths: list[list[Edge]]) -> tuple[PathEvidence, ...]:
        """Per-PATH evidence (spec A2.4.4, Severity 1 finding 3) -- always
        computed directly from `_path_taint`, NEVER through
        `check_source_separation`'s aggregate-across-paths shortcut, so
        qualification can require positively verified provenance AND a
        passing stratum on the SAME path, not a union of the best taint seen
        anywhere with the best strata seen anywhere."""
        return tuple(
            PathEvidence(taint=_path_taint(p, lowered_excluded), strata=path_strata(p))
            for p in paths
        )

    if pre_award:
        sep = check_source_separation(pre_award, row.excluded_from_retrieval)
        return RowEvaluation(
            case_id=row.case_id,
            status="recovered",
            source_separation=sep,
            example_path=[f"{e.edge_type}@{e.valid_from}" for e in pre_award[0]],
            path_evidences=_evidence(pre_award),
        )
    if undated:
        sep = check_source_separation(undated, row.excluded_from_retrieval)
        return RowEvaluation(
            case_id=row.case_id,
            status="undated_only",
            reason="path found but temporally undecidable (spec SS7.2)",
            source_separation=sep,
            example_path=[f"{e.edge_type}@{e.valid_from}" for e in undated[0]],
            path_evidences=_evidence(undated),
        )
    return RowEvaluation(case_id=row.case_id, status="not_recovered")


def _apply_psc_relabel(row: GoldRow, evaluation: RowEvaluation) -> RowEvaluation:
    """Spec A2.3.3: a PSC-sourced row that finds no path is an EXPECTED
    "no trace" -- PSC is a label source only, never ingested for retrieval --
    never a refutation.

    Relabels a `not_recovered` PSC row to `no_trace_by_design`. Every other
    status is left untouched: a PSC row that IS recovered through
    independent register evidence is a genuine recovery, not overridden, and
    an `untestable` PSC row is still a real resolution gap worth its own
    label, not conflated with the PSC caveat.
    """
    if row.is_psc_sourced and evaluation.status == "not_recovered":
        return RowEvaluation(
            case_id=evaluation.case_id,
            status="no_trace_by_design",
            reason=(
                "PSC is a label source only, not ingested for retrieval -- expected "
                "no-trace, not a refutation (spec A2.3.3)"
            ),
            source_separation=evaluation.source_separation,
            example_path=evaluation.example_path,
        )
    return evaluation


def evaluate_row(
    row: GoldRow,
    adj: dict[int, list[Edge]],
    people_by_surname: dict[str, list[Entity]],
    ch_cache: dict,
    max_hops: int,
) -> RowEvaluation:
    """Classify one gold row on its OWN `award_date` as the cutoff.

    Reuses `resolve_supplier` and `find_paths` exactly as Phase C's other
    scripts do, via the shared `_resolve_pair` helper. Note that the BINDING
    case-level test (`evaluate_case`) uses the case's earliest qualifying
    award date, not each row's own -- this function is the standalone,
    per-row view (spec SS2.3's per-row admissibility question), not the
    scored recovery test.
    """
    result = _resolve_pair(row, adj, people_by_surname, ch_cache, max_hops, cutoff=row.award_date)
    return _apply_psc_relabel(row, result)


def evaluate_case(
    case: GoldCase,
    adj: dict[int, list[Edge]],
    people_by_surname: dict[str, list[Entity]],
    ch_cache: dict,
    max_hops: int,
) -> CaseEvaluation:
    """Roll up every row in a case into ONE case-level status (spec A2.1.1, A2.3.2).

    Every row is tested against the CASE's earliest qualifying award date
    (`case.earliest_award_date`), not its own -- the strictest cutoff
    available, since a relationship pre-dating the earliest award
    necessarily pre-dates every later award from the same awardee too.

    A case's status is the STRONGEST status achieved by any of its rows
    (recovered > undated_only > not_recovered > untestable >
    no_trace_by_design): a case counts as recovered if any one of the people
    tied to it produces a pre-award path, even if others don't resolve at
    all. `path_evidences` is the CONCATENATION (never a union or merge) of
    every contributing row's per-path evidence, so qualification
    (`classify_recovered_cases`) can still require positively verified
    provenance and a passing stratum on the SAME path (spec A2.4.4).
    `source_separation` remains a human-readable summary only -- "ok" if any
    contributing row found an independently clean path, else "cannot_verify"
    if any is unverifiable, else "violation" only if every one is proven
    tainted.
    """
    cutoff = case.earliest_award_date
    row_evals = [
        _apply_psc_relabel(
            row, _resolve_pair(row, adj, people_by_surname, ch_cache, max_hops, cutoff)
        )
        for row in case.rows
    ]
    status = max(row_evals, key=lambda r: _STATUS_PRIORITY[r.status]).status

    if status in ("recovered", "undated_only"):
        contributing = [r for r in row_evals if r.status == status]
        seps = [r.source_separation for r in contributing]
        if "ok" in seps:
            sep = "ok"
        elif "cannot_verify" in seps:
            sep = "cannot_verify"
        elif "violation" in seps:
            sep = "violation"
        else:
            sep = "not_applicable"
        path_evidences = tuple(pe for r in contributing for pe in r.path_evidences)
    else:
        sep = "not_applicable"
        path_evidences = ()

    return CaseEvaluation(
        case_key=case.case_key,
        company_number=case.company_number,
        row_count=case.row_count,
        award_count=case.award_count,
        earliest_award_date=case.earliest_award_date.isoformat(),
        row_case_ids=[r.case_id for r in case.rows],
        status=status,
        source_separation=sep,
        row_evaluations=row_evals,
        path_evidences=path_evidences,
    )


@dataclass(frozen=True)
class RecoveredCaseSplit:
    """The four-way, MUTUALLY EXCLUSIVE split of `status == "recovered"`
    cases produced by `classify_recovered_cases`. Every recovered case
    appears in exactly one bucket."""

    qualifying: list[CaseEvaluation]
    circular: list[CaseEvaluation]
    unverifiable: list[CaseEvaluation]
    instrument_limited: list[CaseEvaluation]


def classify_recovered_cases(
    case_evaluations: list[CaseEvaluation],
    stratum_gates: dict[str, StratumGate],
) -> RecoveredCaseSplit:
    """Split `status == "recovered"` cases by PER-PATH qualification (spec
    A2.4.4, A2.4.4/SS3), replacing the two-stage
    split-then-filter that unioned taint and strata across paths (Severity 1
    findings 2 and 3 of the adversarial review).

    A case is `qualifying` iff AT LEAST ONE of its individual `path_evidences`
    independently satisfies `PathEvidence.passes_stratum_gates` -- positively
    verified permitted provenance (taint == "clean", never "cannot_verify" /
    unverifiable and never a proven "tainted" violation) AND a non-empty
    material stratum set AND every stratum that SAME path touches is
    currently passing. This is a per-path AND, never a case-wide union:

      * a single path spanning both a passing and a non-passing stratum does
        NOT qualify merely because one of its strata passes (fixes finding 3a);
      * a clean-but-unsupported-stratum path can never be combined with a
        circular-but-passing-stratum path from a DIFFERENT path on the same
        case to manufacture a false qualification (fixes finding 3b);
      * a path whose only unattested/"cannot_verify" evidence is publication-
        grade cannot rescue a case into CONFIRMED/PARTIAL/REFUTED merely
        because nothing PROVES it is circular -- only a POSITIVELY verified
        path counts (fixes finding 2: fail CLOSED, not open).

    Non-qualifying recovered cases are further bucketed, for transparent
    reporting only (none of these ever count toward CONFIRMED/PARTIAL/
    REFUTED):

      * `circular`   -- every one of its paths is proven `tainted`; SS3
                        proves the case is recoverable ONLY through an
                        excluded source.
      * `unverifiable` -- no path is `tainted` either (not proven circular),
                        but none is positively verified clean -- publication-
                        grade evidence must fail CLOSED here, not credit an
                        unproven provenance.
      * `instrument_limited` -- at least one path IS positively verified
                        clean, but none of its strata are currently passing
                        (e.g. Lords-only while Lords remains unavailable).
    """
    passing = frozenset(
        name for name in MATERIAL_STRATA if stratum_gates.get(name, StratumGate()).passed
    )
    recovered = [c for c in case_evaluations if c.status == "recovered"]

    qualifying: list[CaseEvaluation] = []
    circular: list[CaseEvaluation] = []
    unverifiable: list[CaseEvaluation] = []
    instrument_limited: list[CaseEvaluation] = []

    for case in recovered:
        evidences = case.path_evidences
        if any(pe.passes_stratum_gates(passing) for pe in evidences):
            qualifying.append(case)
        elif evidences and all(pe.taint == "tainted" for pe in evidences):
            circular.append(case)
        elif any(pe.is_positively_verified for pe in evidences):
            instrument_limited.append(case)
        else:
            unverifiable.append(case)

    return RecoveredCaseSplit(
        qualifying=qualifying,
        circular=circular,
        unverifiable=unverifiable,
        instrument_limited=instrument_limited,
    )


def qualifying_strata_touched(
    qualifying_recovered: list[CaseEvaluation],
    stratum_gates: dict[str, StratumGate],
) -> frozenset[str]:
    """The strata actually carried by the PathEvidence entries that made each
    qualifying case qualify -- NOT the display-only `case.strata` union,
    which can include strata from OTHER, non-qualifying paths on the same
    case (spec A2.4.4 reporting must name only the strata the recovery
    itself depends on)."""
    passing = frozenset(
        name for name in MATERIAL_STRATA if stratum_gates.get(name, StratumGate()).passed
    )
    return frozenset().union(
        *(
            pe.strata
            for case in qualifying_recovered
            for pe in case.path_evidences
            if pe.passes_stratum_gates(passing)
        )
    )


def wilson_upper_bound(successes: int, n: int, z: float = _Z_95) -> float:
    """Wilson score interval upper bound for a binomial proportion.

    Spec A2.5: "0/200 is not a zero false-positive rate" -- its approximate
    95% upper bound must always be reported alongside a raw count, never a
    bare zero. This uses the standard Wilson interval (works for any x, not
    just x=0) rather than the simpler rule-of-three heuristic (`3/n`) the
    amendment text illustrates its point with; at x=0, n=200 this gives
    ~1.9% versus the amendment's quoted ~1.5% -- both are approximations of
    the same 95% bound, and this is the more general, standard one.
    """
    if n == 0:
        return 0.0
    phat = successes / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    adjustment = z * ((phat * (1 - phat) / n + z * z / (4 * n * n)) ** 0.5)
    return (centre + adjustment) / denom


def negatives_recovered_from(negative_controls: dict) -> int:
    """Severity 1 finding 4: the strict PRE-AWARD endpoint from the negative
    controls JSON -- the same endpoint definition as `cases_recovered`
    (clean, stratum-qualified, dated-before-the-award paths only).

    `negative_controls["with_path"]` (ANY path, dated or not) must NEVER be
    used here: it would measure `compute_precision`'s denominator at a
    looser endpoint than its numerator, making the ratio meaningless.
    `with_preaward` is the run_negative_controls.py field computed at the
    matching strict endpoint.
    """
    return negative_controls["with_preaward"]


def compute_precision(cases_recovered: int, negatives_recovered: int) -> float:
    """BENCHMARK precision over the constructed case-control sample (spec
    SS5/A2.5) -- a non-gating historical diagnostic (amendment A2.4.1).
    `cases_recovered` (case-level, already circularity- and
    stratum-qualification-filtered, STRICT pre-award endpoint) against a
    spurious hit count from the 200 matched negatives, measured at the SAME
    strict pre-award endpoint (Severity 1 finding 4 -- see
    `negatives_recovered`'s caller in `main()`, which now reads
    `with_preaward`, not `with_path`, so both sides of this ratio share one
    definition of "recovered").

    Spec A2.5 is explicit that this is benchmark precision on a constructed
    ~20:200 sample, NOT expected field precision at real-world prevalence --
    callers must label it as such and report sensitivity / false-positive
    rate as separate figures, never substitute this number for either.

    NOT a gate (Severity 2 finding 7): `classify_outcome` does not accept a
    precision argument at all -- see its docstring for why.
    """
    denom = cases_recovered + negatives_recovered
    return cases_recovered / denom if denom > 0 else 0.0


def classify_outcome(
    cases_recovered: int,
    cases_total: int,
    cases_untestable: int,
    cases_no_trace_by_design: int,
    coverage_gate: CoverageGate,
    stratum_gates: dict[str, StratumGate],
) -> str:
    """Spec A2.1.2 (v2.1) verdict set, RESTRUCTURED by amendment v2.4 for
    per-stratum gating and amendment v2.8 for the sealed cohort's declared
    untestable-by-construction subset, all case-level.

    Strict priority order:

      0a. cases_total != PREREGISTERED_COHORT_SIZE         -> INSUFFICIENT-COHORT
      0b. testable cases too small to be the sealed cohort
          minus its declared-by-construction untestables    -> INSUFFICIENT-COHORT
      1. CoverageGate fails (A2.4.2)                       -> INVALID
      2. no material stratum passes at all                 -> INSTRUMENT-LIMITED
      3. >=4 qualifying cases                              -> CONFIRMED
      4. 0 qualifying cases:
           every material stratum passes (A2.4.4)           -> REFUTED
           otherwise                                        -> INSTRUMENT-LIMITED
      5. 1-3 qualifying cases                               -> PARTIAL
      6. anything else (a caller defect, e.g. a negative
         `cases_recovered`)                                 -> UNDEFINED-OUTCOME

    `cases_recovered` MUST already be filtered by the caller through
    `classify_recovered_cases` (excluding proven-circular recoveries and
    recoveries whose evidence touches no passing stratum, spec SS3/A2.4.4) --
    this function does not (and cannot) re-derive either exclusion from a
    bare count; it is the caller's responsibility, exactly like the retired
    single-gate design required for its temporal gate.

    Branch 3 (CONFIRMED) and branch 5 (PARTIAL) can fire even when NOT every
    material stratum passes, as long as enough cases qualify through the
    strata that DO pass -- "a passing Commons gate must not rescue Lords;
    an unvalidated Lords gate must not erase a verified Commons recovery"
    (A2.4.4). Only REFUTED (branch 4's true-until-any-stratum-fails path)
    demands the full battery: it is impossible to assert "0/20, and we are
    confident the instrument would have found something if it were there"
    while any material stratum remains unvalidated.

    NO PRECISION ARGUMENT (Severity 2 finding 7, resolved): v2.1's locked §6
    table required "'>=80% precision" for CONFIRMED, but that precision was
    always computed from the 200-pair matched-negative diagnostic amendment
    v2.4 A2.4.1 explicitly retired to NON-GATING status ("NEITHER feeds
    classify_outcome nor may appear in the primary results table"). Gating
    CONFIRMED on it was therefore a direct contradiction, not a matter of
    interpretation -- this file's own `compute_precision` docstring already
    asserted precision was non-gating before this fix made the code agree.
    Un-retiring the negatives back to gating status is a spec-amendment
    decision (would need a new dated, reviewed amendment, e.g. v2.9) that a
    benchmark-scoring bugfix has no authority to make unilaterally, so the
    resolution taken here is the other side of the choice the review offered
    ("precision must not consume them"): CONFIRMED is decided by case count
    alone, and `benchmark_precision` is reported purely as a labelled,
    non-gating diagnostic (as A2.4.1 and A2.5 already require).

    Branch 6 (Severity 2 finding 6, UNDEFINED-OUTCOME): with precision
    removed from gating, branches 3/4/5 now exhaustively partition every
    legitimate `cases_recovered` value (0, 1-3, and >=4 out of a cohort whose
    size was already validated by branch 0a/0b) -- the specific ambiguous
    scenario the review found ("4 recoveries at precision 0.79" silently
    read as PARTIAL) cannot recur because precision no longer participates.
    Branch 6 remains as an explicit, honest refusal for any input outside
    that partition (e.g. a negative `cases_recovered` from a caller defect)
    rather than a silent fall-through default -- "the scorer must not invent
    an interpretation" applies to every unexpected input, not only the
    precision case that prompted the finding.
    """
    if cases_total != PREREGISTERED_COHORT_SIZE:
        return "INSUFFICIENT-COHORT"

    testable = cases_total - cases_untestable - cases_no_trace_by_design
    min_expected_testable = PREREGISTERED_COHORT_SIZE - PREREGISTERED_UNTESTABLE_BY_CONSTRUCTION
    if testable < min_expected_testable:
        return "INSUFFICIENT-COHORT"

    if not coverage_gate.passed:
        return "INVALID"

    gates = {name: stratum_gates.get(name, StratumGate()) for name in MATERIAL_STRATA}
    any_stratum_passes = any(g.passed for g in gates.values())
    all_strata_pass = all(g.passed for g in gates.values())

    if not any_stratum_passes:
        return "INSTRUMENT-LIMITED"

    if cases_recovered >= CONFIRM_MIN_CASES:
        return "CONFIRMED"
    if cases_recovered == 0:
        return "REFUTED" if all_strata_pass else "INSTRUMENT-LIMITED"
    if 1 <= cases_recovered < CONFIRM_MIN_CASES:
        return "PARTIAL"
    return "UNDEFINED-OUTCOME"


def country_switch_triggered(outcome: str) -> bool:
    """Spec A2.1.2: COUNTRY_SWITCH is an ACTION, not a verdict.

    Triggered by PARTIAL, REFUTED, or INSTRUMENT-LIMITED -- i.e. by every
    verdict except CONFIRMED, INVALID, INSUFFICIENT-COHORT, and
    UNDEFINED-OUTCOME (a broken pipeline, an inadequately-sized/tested
    cohort, or an input combination the pre-registration does not cover
    licenses no action at all until it is fixed/understood -- switching
    country does not fix a resolver regression, a too-small manifest, or an
    unclassifiable result).
    """
    return outcome in ("PARTIAL", "REFUTED", "INSTRUMENT-LIMITED")


def _run_control_script(script_name: str, extra_args: list[str]) -> dict:
    out_path = REPO_ROOT / "experiments" / f"_gold_benchmark_{script_name}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(REPO_ROOT / "scripts" / script_name),
        "--out",
        str(out_path),
        *extra_args,
    ]
    env = dict(os.environ)
    env["PYTHONPATH"] = f".:{REPO_ROOT / 'src'}"
    subprocess.run(cmd, check=True, cwd=REPO_ROOT, env=env)
    return json.loads(out_path.read_text())


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="data/gold_manifest.csv")
    parser.add_argument("--max-hops", type=int, default=2)
    parser.add_argument("--controls-n", type=int, default=10)
    parser.add_argument("--negatives-n", type=int, default=200)
    parser.add_argument(
        "--coverage-gate-report",
        default="experiments/coverage_gate.json",
        help=(
            "path to the spec A2.4.2 global coverage gate result (supplier-universe CH "
            "coverage, Commons universe coverage). If absent, treated as failing -- see "
            "load_coverage_gate()."
        ),
    )
    parser.add_argument(
        "--stratum-gates-report",
        default="experiments/stratum_gates.json",
        help=(
            "path to the spec A2.4.3 per-material-stratum gate results. If absent, or a "
            "stratum's entry is absent, that stratum defaults to unavailable -- see "
            "load_stratum_gates()."
        ),
    )
    parser.add_argument("--out", default="experiments/gold_benchmark.json")
    parser.add_argument(
        "--skip-controls",
        action="store_true",
        help=(
            "reuse experiments/positive_controls.json and negative_controls.json instead of "
            "re-running them. These are non-gating diagnostics only (spec A2.4.1)."
        ),
    )
    parser.add_argument(
        "--allow-protocol-deviation",
        action="store_true",
        help=(
            "score anyway even if --max-hops or the manifest's case set does not match the "
            "locked/sealed protocol (spec A2.4.5) -- for development/synthetic-manifest runs "
            "only, never for the sealed gold benchmark."
        ),
    )
    args = parser.parse_args()

    manifest_result = load_gold_manifest(args.manifest)
    concentrated_cases = [c for c in manifest_result.cases if c.is_concentrated]
    psc_rows = [r for r in manifest_result.admissible if r.is_psc_sourced]

    # Severity 3 finding 9: refuse to silently score a different manifest
    # subset or a non-locked hop budget as if it were the sealed benchmark.
    protocol_violations = validate_locked_protocol(manifest_result, args.max_hops)
    if protocol_violations:
        for violation in protocol_violations:
            print(f"PROTOCOL: {violation}", file=sys.stderr)
        if not args.allow_protocol_deviation:
            raise SystemExit(
                "Refusing to score: manifest/hop budget does not match the locked, sealed "
                "protocol (spec A2.4.5). Pass --allow-protocol-deviation to score anyway "
                "(development/synthetic manifests only -- never the sealed gold benchmark)."
            )
        print(
            "WARNING: --allow-protocol-deviation set -- scoring despite the violation(s) "
            "above. This run is NOT the sealed gold benchmark.",
            file=sys.stderr,
        )

    print(f"=== GOLD MANIFEST: {args.manifest} ===")
    print(f"admissible rows : {len(manifest_result.admissible)}")
    print(
        f"distinct cases  : {len(manifest_result.cases)}"
        "  (unit of analysis -- awardee company, spec A2.3.2)"
    )
    print(f"inadmissible    : {len(manifest_result.inadmissible)}")
    for row in manifest_result.inadmissible:
        print(f"  REJECTED {row.case_id}: {'; '.join(row.reasons)}")
    if concentrated_cases:
        print(
            "\nconcentrated cases (>1 row -- spec A2.1.1/A2.3.2 requires these listed explicitly):"
        )
        for case in concentrated_cases:
            print(
                f"  {case.case_key}: {case.row_count} rows, {case.award_count} distinct award(s) "
                f"-- {[r.case_id for r in case.rows]}"
            )
    if psc_rows:
        print(
            f"\nPSC-sourced rows (spec A2.3.3, expected unrecoverable by design): "
            f"{len(psc_rows)} -- {[r.case_id for r in psc_rows]}"
        )

    if args.skip_controls:
        positive_controls = json.loads(
            (REPO_ROOT / "experiments" / "positive_controls.json").read_text()
        )
        negative_controls = json.loads(
            (REPO_ROOT / "experiments" / "negative_controls.json").read_text()
        )
    else:
        print("\nrunning graph-derived positive controls (fresh -- NON-GATING, spec A2.4.1)...")
        positive_controls = _run_control_script(
            "run_positive_controls.py", ["--n", str(args.controls_n)]
        )
        print("running negative controls (fresh -- NON-GATING topology snapshot, spec A2.4.1)...")
        negative_controls = _run_control_script(
            "run_negative_controls.py", ["--n", str(args.negatives_n)]
        )

    # Spec A2.4.1: these are HISTORICAL, NON-GATING DIAGNOSTICS. Neither feeds
    # classify_outcome. The graph-derived positive controls are a regression
    # fixture; the negative-pair rate is a topology snapshot, not a
    # population false-positive estimate.
    diagnostic_regression_recovered = positive_controls["retrieved"]
    diagnostic_regression_total = positive_controls["n"]
    # Severity 1 finding 4: `benchmark_precision`'s numerator (`cases_recovered`)
    # is the STRICT pre-award endpoint -- clean, stratum-qualified,
    # dated-before-the-award paths only. Reading `with_path` here (ANY path,
    # dated or not) would measure the denominator at a looser endpoint than
    # the numerator, making the ratio meaningless. `with_preaward` is the
    # matching strict endpoint on the negative side, so both halves of this
    # ratio share exactly one definition of "recovered".
    negatives_recovered = negatives_recovered_from(negative_controls)
    negatives_total = negative_controls["n"]

    # Severity 3 finding 10: bind the gate reports to the CURRENT graph, code
    # commit and manifest -- a gate measured against a different state must
    # fail closed, never silently authorize this run (spec A2.4.5).
    binding = GateBinding(
        code_commit=current_code_commit(),
        graph_hash=compute_graph_hash(),
        manifest_hash=compute_manifest_hash(Path(args.manifest)),
    )
    coverage_gate = load_coverage_gate(Path(args.coverage_gate_report), binding=binding)
    stratum_gates = load_stratum_gates(Path(args.stratum_gates_report), binding=binding)
    if not coverage_gate.passed:
        print(
            f"\nWARNING: coverage gate not passing (report: {args.coverage_gate_report}) -- "
            "spec A2.4.2 requires this before anything else can be evaluated. Outcome is "
            "capped at INVALID.",
            file=sys.stderr,
        )
    unavailable_strata = [name for name in MATERIAL_STRATA if not stratum_gates[name].available]
    if unavailable_strata:
        print(
            f"\nNOTE: stratum gate(s) unavailable: {unavailable_strata} -- spec A2.4.4: REFUTED "
            "can never fire while any material stratum lacks a passing gate.",
            file=sys.stderr,
        )

    people_by_surname: dict[str, list[Entity]] = {}
    for person in Entity.objects.filter(entity_type="person"):
        sn = surname(person.name)
        if sn:
            people_by_surname.setdefault(sn, []).append(person)

    adj = build_adjacency()
    print(f"\ngraph: {Entity.objects.count()} entities, {Edge.objects.count()} edges")

    case_evaluations = [
        evaluate_case(case, adj, people_by_surname, {}, args.max_hops)
        for case in manifest_result.cases
    ]
    row_evaluations = [re for ce in case_evaluations for re in ce.row_evaluations]

    def _count(evals: list, status: str) -> int:
        return sum(1 for e in evals if e.status == status)

    recovered_split = classify_recovered_cases(case_evaluations, stratum_gates)
    qualifying_recovered = recovered_split.qualifying
    circular_recovered = recovered_split.circular
    unverifiable_recovered = recovered_split.unverifiable
    instrument_limited_recovered = recovered_split.instrument_limited
    cases_recovered = len(qualifying_recovered)
    cases_recovered_circular = len(circular_recovered)
    cases_recovered_unverifiable = len(unverifiable_recovered)
    cases_recovered_instrument_limited = len(instrument_limited_recovered)
    qualifying_strata = qualifying_strata_touched(qualifying_recovered, stratum_gates)

    cases_undated_only = _count(case_evaluations, "undated_only")
    cases_not_recovered = _count(case_evaluations, "not_recovered")
    cases_untestable = _count(case_evaluations, "untestable")
    cases_no_trace_by_design = _count(case_evaluations, "no_trace_by_design")

    rows_recovered = _count(row_evaluations, "recovered")
    rows_undated_only = _count(row_evaluations, "undated_only")
    rows_not_recovered = _count(row_evaluations, "not_recovered")
    rows_untestable = _count(row_evaluations, "untestable")
    rows_no_trace_by_design = _count(row_evaluations, "no_trace_by_design")

    # NON-GATING (Severity 2 finding 7): benchmark_precision is a reported
    # diagnostic only -- classify_outcome takes no precision argument. Both
    # sides of the ratio now share the strict pre-award endpoint (Severity 1
    # finding 4).
    benchmark_precision = compute_precision(cases_recovered, negatives_recovered)
    # Denominator is EVERY case, including untestable and no_trace_by_design
    # ones -- named explicitly so a reader cannot mistake a low figure here
    # for evidence of absence when it may just reflect resolution gaps.
    sensitivity_over_all_cases = (
        cases_recovered / len(manifest_result.cases) if manifest_result.cases else 0.0
    )
    false_positive_rate = negatives_recovered / negatives_total if negatives_total else 0.0
    negatives_fp_upper_95 = wilson_upper_bound(negatives_recovered, negatives_total)

    outcome = classify_outcome(
        cases_recovered,
        len(manifest_result.cases),
        cases_untestable,
        cases_no_trace_by_design,
        coverage_gate,
        stratum_gates,
    )
    switch_action = country_switch_triggered(outcome)

    # `violations` is EVERY case with a proven-circular source (recovered or
    # undated_only -- SS3's rule applies regardless of temporal admissibility);
    # `circular_recovered` (above) is the verdict-critical subset.
    violations = [c for c in case_evaluations if c.source_separation == "violation"]
    undated_only_violations = [c for c in violations if c.status == "undated_only"]
    # Case-level SUMMARY diagnostic (display only, via the aggregate
    # `source_separation` field) -- distinct from `unverifiable_recovered`
    # above, which is the PER-PATH exclusion (Severity 1 finding 2) actually
    # subtracted from `cases_recovered`.
    unverifiable_summary = [c for c in case_evaluations if c.source_separation == "cannot_verify"]

    print(f"\n=== PHASE C GOLD BENCHMARK (max {args.max_hops} hops, case-level) ===")
    print(f"cases total (distinct awardees)    : {len(manifest_result.cases)}")
    print(
        f"  recovered (pre-award, qualifying): {cases_recovered}"
        f"  -- strata: {sorted(qualifying_strata) or 'none'}"
    )
    print(
        f"  recovered_circular (SS3 violation): {cases_recovered_circular}"
        "  -- PROVEN circular, EXCLUDED from recovered/CONFIRMED/REFUTED"
    )
    print(
        f"  recovered_unverifiable_provenance : {cases_recovered_unverifiable}"
        "  -- no path positively verified clean, EXCLUDED from recovered (fail CLOSED)"
    )
    print(
        f"  recovered_instrument_limited      : {cases_recovered_instrument_limited}"
        "  -- evidence touches NO passing stratum, EXCLUDED from recovered (A2.4.4)"
    )
    print(
        f"  undated_only (temporal unknown)  : {cases_undated_only}"
        "  -- never recovery, never refutation"
    )
    print(f"  not_recovered (real miss)        : {cases_not_recovered}")
    print(f"  untestable (unresolved)          : {cases_untestable}  -- excluded from denominators")
    print(
        f"  no_trace_by_design (PSC, A2.3.3) : {cases_no_trace_by_design}"
        "  -- expected, never a refutation"
    )
    print(
        f"rows (secondary, non-headline)    : recovered={rows_recovered} "
        f"undated_only={rows_undated_only} not_recovered={rows_not_recovered} "
        f"untestable={rows_untestable} no_trace_by_design={rows_no_trace_by_design}"
    )
    print(f"coverage gate (A2.4.2)             : {'PASS' if coverage_gate.passed else 'FAIL'}")
    print("stratum gates (A2.4.3/A2.4.4):")
    for name in MATERIAL_STRATA:
        g = stratum_gates[name]
        print(
            f"  {name:28s}: {'PASS' if g.passed else 'FAIL'} "
            f"(available={g.available}, retrieval={g.retrieval_passed}, "
            f"temporal={g.temporal_passed})"
        )
    print(
        "\n[non-gating diagnostics, spec A2.4.1 -- historical only, never in the primary "
        "results table]"
    )
    print(
        f"  graph-derived regression controls: {diagnostic_regression_recovered}/"
        f"{diagnostic_regression_total}"
    )
    print(
        f"  negative spurious rate (pre-award, strict): {negatives_recovered}/{negatives_total} "
        f"(95% CI upper bound: {negatives_fp_upper_95:.1%}) -- spec A2.5: never a bare zero"
    )
    print(
        f"  benchmark precision (NOT field precision, NOT gating -- A2.4.1/finding 7): "
        f"{benchmark_precision:.3f}"
    )
    if cases_recovered > 0 and benchmark_precision < CONFIRM_MIN_PRECISION:
        print(
            f"  NOTE: benchmark precision {benchmark_precision:.3f} is below the historical "
            f"{CONFIRM_MIN_PRECISION:.0%} figure -- reported for transparency only; it does NOT "
            "affect the outcome above (finding 7)."
        )
    print(
        f"  sensitivity (recall, over ALL {len(manifest_result.cases)} cases incl. "
        f"untestable/no_trace_by_design): {sensitivity_over_all_cases:.3f}"
    )
    print(
        f"  false_positive_rate              : {false_positive_rate:.3f} "
        f"(95% CI upper bound: {negatives_fp_upper_95:.1%})"
    )
    print(f"\n>>> OUTCOME: {outcome} <<<")
    print(f">>> COUNTRY_SWITCH action triggered: {switch_action} <<<")
    if circular_recovered:
        print(
            f"\nWARNING: spec SS3 source-separation VIOLATION -- "
            f"{cases_recovered_circular} case(s) recovered ONLY through a proven-circular "
            f"path (excluded from the recovered count above): "
            f"{[c.case_key for c in circular_recovered]}"
        )
    if unverifiable_recovered:
        print(
            f"\nNOTE: {cases_recovered_unverifiable} recovered case(s) had no path with "
            f"positively verified permitted provenance (excluded from the recovered count, "
            f"fail-CLOSED per finding 2): {[c.case_key for c in unverifiable_recovered]}"
        )
    if instrument_limited_recovered:
        print(
            f"\nNOTE: {cases_recovered_instrument_limited} recovered case(s) depend entirely on "
            f"an unsupported stratum (spec A2.4.4 -- excluded from the recovered count, not a "
            f"refutation): {[c.case_key for c in instrument_limited_recovered]}"
        )
    if undated_only_violations:
        print(
            f"\nNOTE: {len(undated_only_violations)} undated_only case(s) also carry a proven "
            f"SS3 source-separation violation (does not affect the verdict -- undated_only "
            f"never counts as recovered -- but is a real circularity in that evidence): "
            f"{[c.case_key for c in undated_only_violations]}"
        )
    if unverifiable_summary:
        print(
            f"\nNOTE: source separation could NOT be verified for {len(unverifiable_summary)} "
            f"case(s) (unattested edge on every found path): "
            f"{[c.case_key for c in unverifiable_summary]}"
        )

    report = {
        "outcome": outcome,
        "country_switch_triggered": switch_action,
        "qualifying_strata": sorted(qualifying_strata),
        "benchmark_precision": benchmark_precision,
        "benchmark_precision_note": "non-gating diagnostic only -- see finding 7 / A2.4.1",
        "sensitivity_over_all_cases": sensitivity_over_all_cases,
        "false_positive_rate": false_positive_rate,
        "false_positive_rate_upper_bound_95pct": negatives_fp_upper_95,
        "cases": {
            "total": len(manifest_result.cases),
            "recovered": cases_recovered,
            "recovered_circular": cases_recovered_circular,
            "recovered_unverifiable_provenance": cases_recovered_unverifiable,
            "recovered_instrument_limited": cases_recovered_instrument_limited,
            "undated_only": cases_undated_only,
            "not_recovered": cases_not_recovered,
            "untestable": cases_untestable,
            "no_trace_by_design": cases_no_trace_by_design,
            "recovered_circular_case_keys": [c.case_key for c in circular_recovered],
            "recovered_unverifiable_provenance_case_keys": [
                c.case_key for c in unverifiable_recovered
            ],
            "recovered_instrument_limited_case_keys": [
                c.case_key for c in instrument_limited_recovered
            ],
            "concentrated": [
                {
                    "case_key": c.case_key,
                    "row_count": c.row_count,
                    "award_count": c.award_count,
                    "row_case_ids": c.row_case_ids,
                }
                for c in concentrated_cases
            ],
        },
        "rows": {
            "total": len(manifest_result.admissible),
            "recovered": rows_recovered,
            "undated_only": rows_undated_only,
            "not_recovered": rows_not_recovered,
            "untestable": rows_untestable,
            "no_trace_by_design": rows_no_trace_by_design,
            "psc_sourced": [r.case_id for r in psc_rows],
            "note": "secondary figure only -- spec A2.1.1 forbids row-level as a headline",
        },
        "coverage_gate": {
            "passed": coverage_gate.passed,
            "supplier_universe_passed": coverage_gate.supplier_universe_passed,
            "commons_universe_passed": coverage_gate.commons_universe_passed,
        },
        "stratum_gates": {
            name: {
                "available": stratum_gates[name].available,
                "retrieval_passed": stratum_gates[name].retrieval_passed,
                "temporal_passed": stratum_gates[name].temporal_passed,
                "passed": stratum_gates[name].passed,
            }
            for name in MATERIAL_STRATA
        },
        "non_gating_diagnostics": {
            "note": (
                "Spec A2.4.1: historical diagnostics only. Neither feeds classify_outcome, "
                "neither may appear in a primary results table or a before/after comparison."
            ),
            "graph_derived_regression_controls": {
                "recovered": diagnostic_regression_recovered,
                "total": diagnostic_regression_total,
            },
            "negative_controls_topology_snapshot": {
                "pre_award_strict": negatives_recovered,
                "n": negatives_total,
                "upper_bound_95pct": negatives_fp_upper_95,
                "note": (
                    "pre-award, strict endpoint -- same definition of 'recovered' as the "
                    "positive side of benchmark_precision (finding 4)"
                ),
            },
        },
        "source_separation": {
            "violations": [c.case_key for c in violations],
            "violations_recovered_excluded_from_cases_recovered": [
                c.case_key for c in circular_recovered
            ],
            "violations_undated_only_no_verdict_impact": [
                c.case_key for c in undated_only_violations
            ],
            "unverifiable": [c.case_key for c in unverifiable_summary],
            "known_limits": (
                "Best-effort Attestation.source_name substring match only. Cannot detect an "
                "excluded source that shaped resolution/ingestion without being logged as an "
                "attestation, nor a wording mismatch between excluded_from_retrieval text and "
                "source_name conventions."
            ),
        },
        "inadmissible_rows": [
            {"case_id": r.case_id, "reasons": list(r.reasons)} for r in manifest_result.inadmissible
        ],
        "case_detail": [
            {
                "case_key": c.case_key,
                "company_number": c.company_number,
                "row_count": c.row_count,
                "award_count": c.award_count,
                "earliest_award_date": c.earliest_award_date,
                "row_case_ids": c.row_case_ids,
                "status": c.status,
                "source_separation": c.source_separation,
                "strata": sorted(c.strata),
            }
            for c in case_evaluations
        ],
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
