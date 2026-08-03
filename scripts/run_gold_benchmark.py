"""Run the pre-registered Phase C gold-manifest benchmark -- ONE verdict.

Spec (LOCKED, do not deviate): vault
`02 Projects/Ideas/Decorruptio/05 Specs/phase-c-gold-manifest-preregistration.md`,
including AMENDMENT v2 (temporal evidence ladder), v2.1 (unit of analysis +
revised verdict set), v2.3 (manifest schema semantics + the narrowed case
key) and v2.4 (control battery, per-stratum gating, freeze protocol).
Sections 5-7 and all four amendments are binding.

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

Only a resolved, non-PSC-only, non-circular pair with no path at all, dated
or undated, is a genuine `not_recovered` miss.

VERDICT SET (amendments v2.1 A2.1.2 and v2.4 A2.4.4):

  INSUFFICIENT-COHORT  testable cases fall short of the pre-registered
                       20-case cohort. Checked first.
  INVALID              the CoverageGate fails -- pipeline broken.
  INSTRUMENT-LIMITED   no material stratum passes at all, OR (when 0 cases
                       qualify) at least one material stratum is still
                       unvalidated -- the UK strict hypothesis, or that
                       part of it, is untestable with these sources.
  CONFIRMED / PARTIAL  >=4 (CONFIRMED) or 1-3 (PARTIAL) cases recovered
                       through a PASSING stratum's evidence -- source-
                       qualified: the verdict names which strata the
                       recovered paths belong to (see `filter_by_passing_stratum`
                       and `main()`'s reporting). A case recovered ONLY
                       through an unsupported stratum (e.g. Lords-only,
                       while Lords remains unavailable) does not count here;
                       it is reported separately, per case.
  REFUTED              0 cases recovered AND EVERY material stratum passes
                       its own retrieval and temporal controls (A2.4.4) --
                       the strongest, least available verdict. A passing
                       Commons gate never rescues Lords, and an unvalidated
                       Lords gate never erases a genuine, independently
                       verified Commons recovery (A2.4.4) -- this is why
                       CONFIRMED/PARTIAL use per-stratum qualification while
                       REFUTED requires the whole battery.

COUNTRY_SWITCH IS NOT A VERDICT -- it is an action triggered by PARTIAL,
REFUTED, or INSTRUMENT-LIMITED (never by INSUFFICIENT-COHORT or INVALID).
See `country_switch_triggered`.

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

from scripts.load_gold_manifest import GoldCase, GoldRow, load_gold_manifest  # noqa: E402
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


@dataclass
class RowEvaluation:
    case_id: str
    status: str  # recovered | undated_only | not_recovered | untestable | no_trace_by_design
    reason: str | None = None
    source_separation: str = "not_applicable"
    example_path: list[str] = field(default_factory=list)
    strata: frozenset[str] = frozenset()


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
    strata: frozenset[str] = frozenset()

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
        return (
            self.total is not None
            and self.total > 0
            and self.covered is not None
            and (self.covered / self.total) >= CONTROLS_PASS_FRACTION
        )

    @property
    def commons_universe_passed(self) -> bool:
        return (
            self.commons_total is not None
            and self.commons_total > 0
            and self.commons_covered is not None
            and (self.commons_covered / self.commons_total) >= CONTROLS_PASS_FRACTION
        )

    @property
    def passed(self) -> bool:
        return self.supplier_universe_passed and self.commons_universe_passed


def load_coverage_gate(path: Path) -> CoverageGate:
    """Load the spec A2.4.2 global coverage gate result, if measured.

    Expected JSON contract, produced by a separate coverage-measurement
    process (not implemented here):

        {
          "supplier_universe_covered": int, "supplier_universe_total": int,
          "commons_universe_covered": int, "commons_universe_total": int
        }

    Returns the all-False default (`CoverageGate()`) if the file does not
    exist -- a missing measurement is never an implicit pass.
    """
    if not path.exists():
        return CoverageGate()
    data = json.loads(path.read_text())
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
        return (
            self.retrieval_total is not None
            and self.retrieval_total > 0
            and self.retrieval_recovered is not None
            and (self.retrieval_recovered / self.retrieval_total) >= CONTROLS_PASS_FRACTION
        )

    @property
    def temporal_passed(self) -> bool:
        return (
            self.temporal_total is not None
            and self.temporal_total > 0
            and self.temporal_recovered is not None
            and (self.temporal_recovered / self.temporal_total) >= CONTROLS_PASS_FRACTION
        )

    @property
    def passed(self) -> bool:
        return self.available and self.retrieval_passed and self.temporal_passed


def load_stratum_gates(path: Path) -> dict[str, StratumGate]:
    """Load the spec A2.4.3 per-material-stratum gate results.

    Expected JSON contract -- one entry per material stratum:

        {
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
    all-strata-must-pass check REFUTED depends on.
    """
    gates = {name: StratumGate() for name in MATERIAL_STRATA}
    if not path.exists():
        return gates
    data = json.loads(path.read_text())
    for name in MATERIAL_STRATA:
        entry = data.get(name)
        if not entry:
            continue
        gates[name] = StratumGate(
            available=bool(entry.get("available", False)),
            retrieval_recovered=entry.get("retrieval_recovered"),
            retrieval_total=entry.get("retrieval_total"),
            temporal_recovered=entry.get("temporal_recovered"),
            temporal_total=entry.get("temporal_total"),
        )
    return gates


def _resolve_referrer_entities(
    row: GoldRow, people_by_surname: dict[str, list[Entity]]
) -> list[Entity]:
    """Registry ID first when the manifest supplies one, surname second.

    Mirrors `resolve_supplier`'s own priority order (a registry identifier
    beats name-derived matching) rather than inventing a different rule for
    the person side of the row.
    """
    if row.person_registry_id:
        by_id = list(
            Entity.objects.filter(entity_type="person", registry_id=row.person_registry_id)
        )
        if by_id:
            return by_id
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

    if pre_award:
        sep = check_source_separation(pre_award, row.excluded_from_retrieval)
        strata = frozenset().union(*(path_strata(p) for p in pre_award))
        return RowEvaluation(
            case_id=row.case_id,
            status="recovered",
            source_separation=sep,
            example_path=[f"{e.edge_type}@{e.valid_from}" for e in pre_award[0]],
            strata=strata,
        )
    if undated:
        sep = check_source_separation(undated, row.excluded_from_retrieval)
        strata = frozenset().union(*(path_strata(p) for p in undated))
        return RowEvaluation(
            case_id=row.case_id,
            status="undated_only",
            reason="path found but temporally undecidable (spec SS7.2)",
            source_separation=sep,
            example_path=[f"{e.edge_type}@{e.valid_from}" for e in undated[0]],
            strata=strata,
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
    all. `strata` is the UNION of material strata across every row
    contributing to that winning status (spec A2.4.4 source-qualification).
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
        strata = (
            frozenset().union(*(r.strata for r in contributing)) if contributing else frozenset()
        )
    else:
        sep = "not_applicable"
        strata = frozenset()

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
        strata=strata,
    )


def split_recovered_by_source_separation(
    case_evaluations: list[CaseEvaluation],
) -> tuple[list[CaseEvaluation], list[CaseEvaluation]]:
    """Split `status == "recovered"` cases into (clean, circular).

    Adversarial-review defect: a case whose `source_separation == "violation"`
    means `check_source_separation` has already PROVEN every path found for
    it is attested SOLELY by a source that row's own `excluded_from_retrieval`
    names -- exactly the circularity spec SS3 exists to rule out ("if a
    relationship is only recoverable because we ingested the newspaper
    article that revealed it, we have measured our own ingest, not the
    registers"). Counting such a case toward CONFIRMED would let the
    project's own journalism/inquiry ingest manufacture an affirmative,
    reputation-bearing claim about a named person or company.

    `clean` is every other recovered case ("ok" -- an independent, permitted
    path exists -- or "cannot_verify", where nothing PROVES circularity, only
    an unattested edge). Only `clean` may ever count toward
    CONFIRMED/PARTIAL/REFUTED thresholds; `circular` must be reported
    prominently and never silently folded back in.

    If a case has both a clean and a tainted path, `evaluate_case`'s own
    roll-up already resolves this: source_separation is "ok" the moment ANY
    contributing row's path is clean, so such a case lands in `clean` here
    too -- the clean path carries it, as required.
    """
    recovered = [c for c in case_evaluations if c.status == "recovered"]
    circular = [c for c in recovered if c.source_separation == "violation"]
    clean = [c for c in recovered if c.source_separation != "violation"]
    return clean, circular


def filter_by_passing_stratum(
    cases: list[CaseEvaluation],
    stratum_gates: dict[str, StratumGate],
) -> tuple[list[CaseEvaluation], list[CaseEvaluation]]:
    """Split recovered (already circularity-cleaned) cases into (qualifying,
    instrument_limited) by whether their evidence touches a PASSING stratum.

    Spec A2.4.4: "recovered strict paths must belong to passing strata" --
    a case whose recovered evidence touches NO passing stratum (e.g. its
    only path is Lords-only, and Lords remains unavailable per A2.4.2)
    cannot count toward CONFIRMED/PARTIAL/REFUTED. It is `instrument_limited`
    FOR THAT CASE, reported separately, never silently dropped.

    A case whose evidence touches AT LEAST ONE passing stratum qualifies --
    even if it ALSO touches an unsupported one -- because "an unvalidated
    Lords gate must not erase a genuine, independently verified Commons
    recovery" (A2.4.4).
    """
    passing = frozenset(
        name for name in MATERIAL_STRATA if stratum_gates.get(name, StratumGate()).passed
    )
    qualifying = [c for c in cases if c.strata & passing]
    instrument_limited = [c for c in cases if not (c.strata & passing)]
    return qualifying, instrument_limited


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


def compute_precision(cases_recovered: int, negatives_recovered: int) -> float:
    """BENCHMARK precision over the constructed case-control sample (spec
    SS5/A2.5) -- itself a non-gating historical diagnostic as of amendment
    A2.4.1. `cases_recovered` (case-level, already circularity- and
    stratum-qualification-filtered) against a spurious hit count from the
    200 matched negatives.

    Spec A2.5 is explicit that this is benchmark precision on a constructed
    ~20:200 sample, NOT expected field precision at real-world prevalence --
    callers must label it as such and report sensitivity / false-positive
    rate as separate figures, never substitute this number for either.
    """
    denom = cases_recovered + negatives_recovered
    return cases_recovered / denom if denom > 0 else 0.0


def classify_outcome(
    cases_recovered: int,
    cases_total: int,
    cases_untestable: int,
    cases_no_trace_by_design: int,
    precision: float,
    coverage_gate: CoverageGate,
    stratum_gates: dict[str, StratumGate],
) -> str:
    """Spec A2.1.2 (v2.1) verdict set, RESTRUCTURED by amendment v2.4 for
    per-stratum gating, all case-level.

    Strict priority order:

      0. testable cases < PREREGISTERED_COHORT_SIZE      -> INSUFFICIENT-COHORT
      1. CoverageGate fails (A2.4.2)                      -> INVALID
      2. no material stratum passes at all                -> INSTRUMENT-LIMITED
      3. >=4 qualifying cases & >=80% precision            -> CONFIRMED
      4. 0 qualifying cases:
           every material stratum passes (A2.4.4)          -> REFUTED
           otherwise                                       -> INSTRUMENT-LIMITED
      5. 1-3 qualifying cases                              -> PARTIAL

    `cases_recovered` MUST already be filtered by the caller through BOTH
    `split_recovered_by_source_separation` (excluding proven-circular
    recoveries, spec SS3) AND `filter_by_passing_stratum` (excluding
    recoveries whose evidence touches no passing stratum, spec A2.4.4) --
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
    """
    testable = cases_total - cases_untestable - cases_no_trace_by_design
    if testable < PREREGISTERED_COHORT_SIZE:
        return "INSUFFICIENT-COHORT"

    if not coverage_gate.passed:
        return "INVALID"

    gates = {name: stratum_gates.get(name, StratumGate()) for name in MATERIAL_STRATA}
    any_stratum_passes = any(g.passed for g in gates.values())
    all_strata_pass = all(g.passed for g in gates.values())

    if not any_stratum_passes:
        return "INSTRUMENT-LIMITED"

    if cases_recovered >= CONFIRM_MIN_CASES and precision >= CONFIRM_MIN_PRECISION:
        return "CONFIRMED"
    if cases_recovered == 0:
        return "REFUTED" if all_strata_pass else "INSTRUMENT-LIMITED"
    return "PARTIAL"


def country_switch_triggered(outcome: str) -> bool:
    """Spec A2.1.2: COUNTRY_SWITCH is an ACTION, not a verdict.

    Triggered by PARTIAL, REFUTED, or INSTRUMENT-LIMITED -- i.e. by every
    verdict except CONFIRMED, INVALID, and INSUFFICIENT-COHORT (a broken
    pipeline or an inadequately-sized/tested cohort licenses no action at
    all until it is fixed -- switching country does not fix a resolver
    regression or a too-small manifest).
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
    args = parser.parse_args()

    manifest_result = load_gold_manifest(args.manifest)
    concentrated_cases = [c for c in manifest_result.cases if c.is_concentrated]
    psc_rows = [r for r in manifest_result.admissible if r.is_psc_sourced]

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
    negatives_recovered = negative_controls["with_path"]
    negatives_total = negative_controls["n"]

    coverage_gate = load_coverage_gate(Path(args.coverage_gate_report))
    stratum_gates = load_stratum_gates(Path(args.stratum_gates_report))
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

    clean_recovered, circular_recovered = split_recovered_by_source_separation(case_evaluations)
    qualifying_recovered, instrument_limited_recovered = filter_by_passing_stratum(
        clean_recovered, stratum_gates
    )
    cases_recovered = len(qualifying_recovered)
    cases_recovered_circular = len(circular_recovered)
    cases_recovered_instrument_limited = len(instrument_limited_recovered)
    qualifying_strata = (
        frozenset().union(*(c.strata for c in qualifying_recovered))
        if qualifying_recovered
        else frozenset()
    )

    cases_undated_only = _count(case_evaluations, "undated_only")
    cases_not_recovered = _count(case_evaluations, "not_recovered")
    cases_untestable = _count(case_evaluations, "untestable")
    cases_no_trace_by_design = _count(case_evaluations, "no_trace_by_design")

    rows_recovered = _count(row_evaluations, "recovered")
    rows_undated_only = _count(row_evaluations, "undated_only")
    rows_not_recovered = _count(row_evaluations, "not_recovered")
    rows_untestable = _count(row_evaluations, "untestable")
    rows_no_trace_by_design = _count(row_evaluations, "no_trace_by_design")

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
        benchmark_precision,
        coverage_gate,
        stratum_gates,
    )
    switch_action = country_switch_triggered(outcome)

    # `violations` is EVERY case with a proven-circular source (recovered or
    # undated_only -- SS3's rule applies regardless of temporal admissibility);
    # `circular_recovered` (above) is the verdict-critical subset.
    violations = [c for c in case_evaluations if c.source_separation == "violation"]
    undated_only_violations = [c for c in violations if c.status == "undated_only"]
    unverifiable = [c for c in case_evaluations if c.source_separation == "cannot_verify"]

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
        f"  negative spurious rate (any path): {negatives_recovered}/{negatives_total} "
        f"(95% CI upper bound: {negatives_fp_upper_95:.1%}) -- spec A2.5: never a bare zero"
    )
    print(f"  benchmark precision (NOT field precision): {benchmark_precision:.3f}")
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
    if unverifiable:
        print(
            f"\nNOTE: source separation could NOT be verified for {len(unverifiable)} case(s) "
            f"(unattested edge on every found path): {[c.case_key for c in unverifiable]}"
        )

    report = {
        "outcome": outcome,
        "country_switch_triggered": switch_action,
        "qualifying_strata": sorted(qualifying_strata),
        "benchmark_precision": benchmark_precision,
        "sensitivity_over_all_cases": sensitivity_over_all_cases,
        "false_positive_rate": false_positive_rate,
        "false_positive_rate_upper_bound_95pct": negatives_fp_upper_95,
        "cases": {
            "total": len(manifest_result.cases),
            "recovered": cases_recovered,
            "recovered_circular": cases_recovered_circular,
            "recovered_instrument_limited": cases_recovered_instrument_limited,
            "undated_only": cases_undated_only,
            "not_recovered": cases_not_recovered,
            "untestable": cases_untestable,
            "no_trace_by_design": cases_no_trace_by_design,
            "recovered_circular_case_keys": [c.case_key for c in circular_recovered],
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
                "any_path": negatives_recovered,
                "n": negatives_total,
                "upper_bound_95pct": negatives_fp_upper_95,
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
            "unverifiable": [c.case_key for c in unverifiable],
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
