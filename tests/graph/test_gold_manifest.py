"""Tests for the Phase C gold-manifest loader and pre-registered benchmark runner.

Spec (LOCKED): vault
`02 Projects/Ideas/Decorruptio/05 Specs/phase-c-gold-manifest-preregistration.md`,
including amendments v2.1 (unit of analysis) and v2.3 (manifest schema
semantics + the narrowed case key).

These tests exist because Phase C v1 conflated outcomes that are genuinely
different -- treating "path found" as proof of truth, and "endpoint never
resolved" as a refutation -- and the project had to retract a result because
of it. Every test below asserts one of those specific failure modes cannot
slip through here unnoticed.

All manifest data here is synthetic (SYNTH-* case IDs, example.invalid URLs,
placeholder names) -- never a real person, company, or case.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from scripts.load_gold_manifest import GoldCase, GoldRow, load_gold_manifest
from scripts.phase_c_paths import build_adjacency, surname
from scripts.run_gold_benchmark import (
    MATERIAL_STRATA,
    STRATUM_CH_OFFICER,
    STRATUM_COMMONS,
    STRATUM_LORDS,
    CaseEvaluation,
    CoverageGate,
    StratumGate,
    check_source_separation,
    classify_edge_stratum,
    classify_outcome,
    compute_precision,
    country_switch_triggered,
    evaluate_case,
    evaluate_row,
    filter_by_passing_stratum,
    load_coverage_gate,
    load_stratum_gates,
    path_strata,
    split_recovered_by_source_separation,
    wilson_upper_bound,
)

from uncorrupt.graph.models import Attestation, Edge, Entity

MANIFEST_HEADER = (
    "case_id,person_name,person_registry_id,company_name,company_number,"
    "relationship_type,established_by,label_source_url,award_date,"
    "relationship_start,excluded_from_retrieval,intermediary_company_number,"
    "awardee_confirmed,held_office_at_award,office_holding_start_date\n"
)

# Appended to every existing test row that doesn't specifically exercise the
# office-holding criterion (spec SS2.5/A2.2.2): office held well before any
# award_date used elsewhere in this file, so it never trips an unrelated
# test's admissibility.
_OFFICE_OK_SUFFIX = ",yes,2010-01-01"


def _write_manifest(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "manifest.csv"
    path.write_text(MANIFEST_HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return path


class TestLoadGoldManifest:
    """Loader admissibility (spec SS2, A2.3.1) and schema (spec SS4) checks."""

    def test_admissible_row_is_loaded_and_company_number_normalised(self, tmp_path):
        """GIVEN a row that satisfies every spec SS2/A2.3.1 admissibility criterion
        WHEN the manifest is loaded
        THEN it is admissible and its company number is zero-padded to 8
        characters by the project's canonical CH normaliser."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-001,Jane Testperson,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,yes,yes,2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 1
        assert result.admissible[0].company_number == "01234567"

    def test_missing_company_number_is_rejected(self, tmp_path):
        """GIVEN a row with a blank company_number
        WHEN the manifest is loaded
        THEN it is inadmissible with a reason citing the missing company number."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-002,Jane Testperson,,Example Holdings Ltd,,directorship,"
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,yes,yes,2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 0
        assert any("missing company_number" in r for r in result.inadmissible[0].reasons)

    def test_missing_award_date_is_rejected(self, tmp_path):
        """GIVEN a row with a blank award_date
        WHEN the manifest is loaded
        THEN it is inadmissible with a reason citing the missing award date."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-003,Jane Testperson,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,,2018-01-15,,,yes,yes,2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 0
        assert any("missing or unparseable award_date" in r for r in result.inadmissible[0].reasons)

    def test_relationship_start_after_award_date_is_rejected(self, tmp_path):
        """GIVEN a row whose relationship_start is AFTER its award_date
        WHEN the manifest is loaded
        THEN it is inadmissible -- the relationship does not pre-date the award (spec SS2.4)."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-004,Jane Testperson,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,2019-01-01,2020-05-01,,,yes,yes,2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 0
        assert any("does not pre-date" in r for r in result.inadmissible[0].reasons)

    def test_relationship_start_unknown_is_rejected(self, tmp_path):
        """GIVEN a row whose relationship_start is the literal string 'unknown'
        WHEN the manifest is loaded
        THEN it is inadmissible -- pre-award cannot be established (spec SS2.4)."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-005,Jane Testperson,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,2021-06-01,unknown,,,yes,yes,2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 0
        assert any("undecidable" in r for r in result.inadmissible[0].reasons)

    def test_missing_label_source_is_rejected(self, tmp_path):
        """GIVEN a row with a blank label_source_url
        WHEN the manifest is loaded
        THEN it is inadmissible with a reason citing the missing label source."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-006,Jane Testperson,,Example Holdings Ltd,1234567,directorship,"
                "journalism,,2021-06-01,2018-01-15,,,yes,yes,2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 0
        assert any("missing label_source_url" in r for r in result.inadmissible[0].reasons)

    def test_missing_required_column_raises_loudly(self, tmp_path):
        """GIVEN a manifest CSV missing a spec SS4 required column
        WHEN it is loaded
        THEN a ValueError is raised naming the missing column, before any row
        is read (spec SS7.3 -- two of Phase C v1's four defects were wrong
        column names)."""
        path = tmp_path / "bad.csv"
        path.write_text("case_id,person_name,company_number\nX,Y,1\n", encoding="utf-8")
        with pytest.raises(ValueError, match="missing required column"):
            load_gold_manifest(path)

    def test_duplicate_case_id_is_rejected(self, tmp_path):
        """GIVEN two rows sharing the same case_id
        WHEN the manifest is loaded
        THEN the second occurrence is inadmissible as a duplicate, and the
        first is still admissible."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-007,A,,Example Ltd,1234567,directorship,journalism,"
                "https://example.invalid/a,2021-06-01,2018-01-15,,,yes,yes,2010-01-01",
                "SYNTH-007,B,,Other Ltd,7654321,directorship,journalism,"
                "https://example.invalid/b,2021-06-01,2018-01-15,,,yes,yes,2010-01-01",
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 1
        assert any("duplicate case_id" in r for r in result.inadmissible[0].reasons)

    def test_awardee_not_confirmed_is_rejected(self, tmp_path):
        """GIVEN a row where awardee_confirmed is explicitly 'no' (e.g. the
        donations family's raw, unverified company_number)
        WHEN the manifest is loaded
        THEN it is inadmissible -- company_number is never guessed at as the
        awardee (spec A2.3.1)."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-008,Jane Testperson,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,no,yes,2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 0
        assert any("not confirmed as the awardee" in r for r in result.inadmissible[0].reasons)

    def test_blank_awardee_confirmed_is_rejected(self, tmp_path):
        """GIVEN a row where awardee_confirmed is left blank
        WHEN the manifest is loaded
        THEN it is inadmissible -- a blank is never treated as an implicit
        confirmation (spec A2.3.1)."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-009,Jane Testperson,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,,yes,2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 0
        assert any("not confirmed as the awardee" in r for r in result.inadmissible[0].reasons)

    def test_intermediary_company_number_is_normalised_when_present(self, tmp_path):
        """GIVEN a row recording a donor/linking entity's number in
        intermediary_company_number (spec A2.3.1 -- the donations family's
        original reading, now corrected)
        WHEN the manifest is loaded
        THEN the intermediary number is normalised to 8 characters like any
        other company number, independently of the awardee company_number."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-014,Robin Donorlinked,,Confirmed Awardee Ltd,4445556,donation,"
                "journalism,https://example.invalid/a,2021-09-01,2019-11-01,,999888,yes,"
                "yes,2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 1
        assert result.admissible[0].company_number == "04445556"
        assert result.admissible[0].intermediary_company_number == "00999888"

    def test_psc_sourced_row_is_admissible_and_flagged(self, tmp_path):
        """GIVEN a row whose established_by is 'PSC'
        WHEN the manifest is loaded
        THEN it is admissible (not rejected, not silently dropped) AND its
        is_psc_sourced property is True (spec A2.3.3 -- flagged, never
        silently kept unflagged either)."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-015,Kim Sample,,Placeholder Services Ltd,9999999,shareholding,"
                "PSC,https://example.invalid/a,2021-01-10,2019-03-01,,,yes,yes,2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 1
        assert result.admissible[0].is_psc_sourced is True

    def test_rows_sharing_awardee_collapse_into_one_case_across_different_awards(self, tmp_path):
        """GIVEN two admissible rows against the SAME awardee company_number
        but DIFFERENT award_date (e.g. PPE Medpro's two separate DHSC
        awards arising from one underlying relationship)
        WHEN the manifest is loaded
        THEN they collapse into exactly ONE case subsuming 2 distinct
        awards -- spec A2.3.2 narrows the case key to the awardee company
        alone, dropping award_date from the key entirely."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-010,Person One,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,yes,"
                "yes,2010-01-01",
                "SYNTH-011,Person Two,,Example Holdings Ltd,1234567,shareholding,"
                "inquiry,https://example.invalid/b,2021-08-15,2019-02-20,,,yes,"
                "yes,2010-01-01",
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 2
        assert len(result.cases) == 1
        assert result.cases[0].row_count == 2
        assert result.cases[0].award_count == 2
        assert result.cases[0].earliest_award_date == date(2021, 6, 1)

    def test_distinct_awardee_companies_are_separate_cases(self, tmp_path):
        """GIVEN two admissible rows with different awardee company numbers
        WHEN the manifest is loaded
        THEN they form two distinct, non-concentrated cases."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-012,Person One,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,yes,"
                "yes,2010-01-01",
                "SYNTH-013,Person Two,,Other Traders Ltd,7654321,directorship,"
                "inquiry,https://example.invalid/b,2021-06-01,2019-02-20,,,yes,"
                "yes,2010-01-01",
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.cases) == 2
        assert all(not c.is_concentrated for c in result.cases)

    def test_held_office_at_award_no_is_out_of_scope_not_inadmissible(self, tmp_path):
        """GIVEN a row where held_office_at_award is explicitly 'no' (the
        motivating example: a 1997 directorship, a 2000 grant, election not
        until 2016)
        WHEN the manifest is loaded
        THEN the row is neither admissible nor inadmissible -- it is
        OUT OF SCOPE (spec A2.2.2): there was no public function to
        influence at the time of the award."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-016,Sam Notyetelected,,Grant Recipient Ltd,5556667,directorship,"
                "inquiry,https://example.invalid/a,2000-01-01,1997-01-01,,,yes,no,2016-05-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 0
        assert len(result.inadmissible) == 0
        assert len(result.out_of_scope) == 1
        assert result.out_of_scope[0].case_id == "SYNTH-016"

    def test_office_holding_start_date_after_award_is_out_of_scope_even_if_held_office_says_yes(
        self, tmp_path
    ):
        """GIVEN held_office_at_award is 'yes' but office_holding_start_date
        itself is strictly AFTER award_date (a curation inconsistency)
        WHEN the manifest is loaded
        THEN the row is OUT OF SCOPE, not admissible -- the date is not
        overridden by an inconsistent 'yes' claim, and this is still not a
        data defect worth rejecting outright: the underlying fact (no
        office at the time) is what governs."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-017,Sam Notyetelected,,Grant Recipient Ltd,5556667,directorship,"
                "inquiry,https://example.invalid/a,2000-01-01,1997-01-01,,,yes,yes,2016-05-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 0
        assert len(result.out_of_scope) == 1
        assert "post-dates" in result.out_of_scope[0].reason

    def test_office_held_before_award_is_admissible(self, tmp_path):
        """GIVEN held_office_at_award is 'yes' and office_holding_start_date
        is before the award_date
        WHEN the manifest is loaded
        THEN the row is admissible -- spec SS2.5 is satisfied, not merely
        SS2.1-SS2.4."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-018,Jane Testperson,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,yes,"
                "yes,2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 1
        assert result.admissible[0].office_holding_start_date == date(2010, 1, 1)

    def test_blank_held_office_at_award_is_inadmissible(self, tmp_path):
        """GIVEN held_office_at_award is left blank
        WHEN the manifest is loaded
        THEN the row is INADMISSIBLE (a data defect -- we don't know either
        way), never treated as an implicit 'no' routed to out-of-scope."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-019,Jane Testperson,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,yes,"
                ",2010-01-01"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 0
        assert len(result.out_of_scope) == 0
        assert any(
            "held_office_at_award must be explicitly" in r for r in result.inadmissible[0].reasons
        )

    def test_missing_office_holding_start_date_is_inadmissible(self, tmp_path):
        """GIVEN office_holding_start_date is left blank
        WHEN the manifest is loaded
        THEN the row is INADMISSIBLE with a reason citing the missing date."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-020,Jane Testperson,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,yes,yes,"
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 0
        assert any(
            "missing or unparseable office_holding_start_date" in r
            for r in result.inadmissible[0].reasons
        )


def _gold_row(**overrides) -> GoldRow:
    defaults = dict(
        case_id="SYNTH-100",
        person_name="Jane Testperson",
        person_registry_id=None,
        company_name="Example Holdings Ltd",
        company_number="01234567",
        relationship_type="directorship",
        established_by="journalism",
        label_source_url="https://example.invalid/a",
        award_date=date(2021, 6, 1),
        relationship_start=date(2018, 1, 15),
        excluded_from_retrieval=(),
        intermediary_company_number=None,
        office_holding_start_date=date(2015, 1, 1),
    )
    defaults.update(overrides)
    return GoldRow(**defaults)


class TestGoldCase:
    """spec A2.3.2: case-level award/row counts, computed from constituent rows."""

    def test_award_count_and_earliest_date_computed_from_rows(self):
        """GIVEN a GoldCase built from two rows with different award dates
        WHEN its award_count, row_count and earliest_award_date are read
        THEN they reflect 2 distinct awards, 2 rows, and the earlier date."""
        row_a = _gold_row(case_id="A", award_date=date(2021, 6, 1))
        row_b = _gold_row(case_id="B", award_date=date(2021, 8, 15))
        case = GoldCase(company_number="01234567", rows=(row_a, row_b))

        assert case.row_count == 2
        assert case.award_count == 2
        assert case.earliest_award_date == date(2021, 6, 1)

    def test_award_count_is_one_when_all_rows_share_the_same_award(self):
        """GIVEN a GoldCase built from two rows with the SAME award_date
        WHEN its award_count is read
        THEN it is 1 -- multiple people on one award do not inflate the
        award count."""
        row_a = _gold_row(case_id="A", award_date=date(2021, 6, 1))
        row_b = _gold_row(case_id="B", award_date=date(2021, 6, 1))
        case = GoldCase(company_number="01234567", rows=(row_a, row_b))

        assert case.award_count == 1


def _all_passing_stratum_gates() -> dict[str, StratumGate]:
    """Every material stratum passing -- retrieval and temporal both clear
    the >=90% bar, and each is marked available."""
    return {
        name: StratumGate(
            available=True,
            retrieval_recovered=10,
            retrieval_total=10,
            temporal_recovered=10,
            temporal_total=10,
        )
        for name in MATERIAL_STRATA
    }


def _passing_coverage_gate() -> CoverageGate:
    return CoverageGate(covered=10, total=10, commons_covered=10, commons_total=10)


class TestClassifyOutcome:
    """Spec A2.1.2 (v2.1) verdict set, case-level, RESTRUCTURED by amendment
    v2.4 for per-stratum gating, plus the INSUFFICIENT-COHORT guard added
    after an adversarial review found the original five verdicts had no
    floor on how many cases were actually tested.

    Unless a test is specifically exercising the cohort-size guard, all
    tests here use a "healthy" cohort (20 total, 0 untestable, 0
    no_trace_by_design) so the verdict logic under test is isolated from the
    cohort-size guard.
    """

    def test_invalid_fires_when_coverage_gate_fails(self):
        """GIVEN the CoverageGate fails (e.g. supplier-universe CH officer
        rosters are incomplete)
        WHEN the outcome is classified
        THEN it is INVALID regardless of cases recovered or stratum gates --
        spec A2.4.2: the pipeline itself is broken."""
        outcome = classify_outcome(
            cases_recovered=20,
            cases_total=20,
            cases_untestable=0,
            cases_no_trace_by_design=0,
            precision=1.0,
            coverage_gate=CoverageGate(),  # all-False default
            stratum_gates=_all_passing_stratum_gates(),
        )
        assert outcome == "INVALID"

    def test_instrument_limited_fires_when_no_stratum_passes_at_all(self):
        """GIVEN the coverage gate passes but NOT ONE material stratum has a
        passing gate
        WHEN the outcome is classified
        THEN it is INSTRUMENT-LIMITED -- nothing about the instrument has
        been validated at all."""
        outcome = classify_outcome(
            cases_recovered=0,
            cases_total=20,
            cases_untestable=0,
            cases_no_trace_by_design=0,
            precision=0.0,
            coverage_gate=_passing_coverage_gate(),
            stratum_gates={name: StratumGate() for name in MATERIAL_STRATA},
        )
        assert outcome == "INSTRUMENT-LIMITED"

    def test_refuted_requires_every_material_stratum_to_pass(self):
        """ADVERSARIAL TEST -- sneak past REFUTED via one unvalidated stratum.

        GIVEN 0 qualifying cases recovered, the coverage gate passes, and
        Commons + CH-officer strata pass but Lords remains `available=False`
        (spec A2.4.2: "Lords source coverage -- gating -- currently
        unavailable") -- i.e. EVERYTHING else about the run looks clean
        WHEN the outcome is classified
        THEN it is INSTRUMENT-LIMITED, NEVER REFUTED -- spec A2.4.4: REFUTED
        is available only if EVERY material stratum passes its own
        retrieval and temporal controls. A passing Commons+CH-officer
        battery cannot rescue an unvalidated Lords stratum into a full
        refutation."""
        gates = _all_passing_stratum_gates()
        gates[STRATUM_LORDS] = StratumGate(available=False)

        outcome = classify_outcome(
            cases_recovered=0,
            cases_total=20,
            cases_untestable=0,
            cases_no_trace_by_design=0,
            precision=0.0,
            coverage_gate=_passing_coverage_gate(),
            stratum_gates=gates,
        )

        assert outcome == "INSTRUMENT-LIMITED"

    def test_refuted_fires_when_every_stratum_genuinely_passes(self):
        """GIVEN 0 qualifying cases recovered, coverage gate passing, AND
        every one of the three material strata passing its own retrieval
        and temporal controls
        WHEN the outcome is classified
        THEN it is REFUTED -- the full battery is the only thing that makes
        this the strongest, least-available verdict."""
        outcome = classify_outcome(
            cases_recovered=0,
            cases_total=20,
            cases_untestable=0,
            cases_no_trace_by_design=0,
            precision=0.0,
            coverage_gate=_passing_coverage_gate(),
            stratum_gates=_all_passing_stratum_gates(),
        )
        assert outcome == "REFUTED"

    def test_confirmed_reachable_via_passing_strata_even_with_lords_unvalidated(self):
        """GIVEN >=4 qualifying cases recovered (already filtered by the
        caller to touch only PASSING strata) and >=80% precision, while
        Commons + CH-officer pass but Lords remains unavailable
        WHEN the outcome is classified
        THEN it is CONFIRMED -- spec A2.4.4: 'an unvalidated Lords gate must
        not erase a genuine, independently verified Commons recovery'.
        CONFIRMED/PARTIAL are source-qualified per passing stratum, unlike
        REFUTED which needs the whole battery."""
        gates = _all_passing_stratum_gates()
        gates[STRATUM_LORDS] = StratumGate(available=False)

        outcome = classify_outcome(
            cases_recovered=4,
            cases_total=20,
            cases_untestable=0,
            cases_no_trace_by_design=0,
            precision=0.80,
            coverage_gate=_passing_coverage_gate(),
            stratum_gates=gates,
        )

        assert outcome == "CONFIRMED"

    def test_partial_fires_for_one_to_three_qualifying_cases(self):
        """GIVEN every stratum passing but qualifying cases recovered is
        short of the CONFIRMED threshold without being exactly zero
        WHEN the outcome is classified
        THEN it is PARTIAL -- real traces below the pre-registered
        confirmation bar, never rendered as a confirmation."""
        outcome = classify_outcome(
            cases_recovered=2,
            cases_total=20,
            cases_untestable=0,
            cases_no_trace_by_design=0,
            precision=1.0,
            coverage_gate=_passing_coverage_gate(),
            stratum_gates=_all_passing_stratum_gates(),
        )
        assert outcome == "PARTIAL"

    def test_insufficient_cohort_blocks_a_false_refuted_when_every_case_is_untestable(self):
        """ADVERSARIAL TEST -- sneak past REFUTED via an all-untestable cohort.

        GIVEN all 20 gold cases fail to resolve (a resolver regression, or an
        ingestion gap for exactly those companies) so cases_recovered == 0,
        while the coverage gate and every stratum gate -- measured on a
        disjoint entity set -- pass independently
        WHEN the outcome is classified
        THEN it is INSUFFICIENT-COHORT, never REFUTED -- a testable
        denominator of 0 can never support "0/20 recovered" as a refutation
        of H1; it is a resolver problem, not evidence against the
        hypothesis."""
        outcome = classify_outcome(
            cases_recovered=0,
            cases_total=20,
            cases_untestable=20,
            cases_no_trace_by_design=0,
            precision=0.0,
            coverage_gate=_passing_coverage_gate(),
            stratum_gates=_all_passing_stratum_gates(),
        )
        assert outcome == "INSUFFICIENT-COHORT"

    def test_insufficient_cohort_blocks_a_false_confirmed_on_a_small_raw_manifest(self):
        """ADVERSARIAL TEST -- sneak past CONFIRMED via a too-small manifest.

        GIVEN only 10 total cases in the manifest (half the pre-registered
        20) with 8 recovered and full precision/coverage/stratum-gate pass
        WHEN the outcome is classified
        THEN it is INSUFFICIENT-COHORT, never CONFIRMED -- the locked
        '>=4/20' bar is calibrated to a 20-case cohort; applying it to a
        smaller, unrepresentative one overstates the evidence."""
        outcome = classify_outcome(
            cases_recovered=8,
            cases_total=10,
            cases_untestable=0,
            cases_no_trace_by_design=0,
            precision=1.0,
            coverage_gate=_passing_coverage_gate(),
            stratum_gates=_all_passing_stratum_gates(),
        )
        assert outcome == "INSUFFICIENT-COHORT"

    def test_insufficient_cohort_counts_no_trace_by_design_cases_as_untested_too(self):
        """GIVEN a full 20-case manifest where 15 are PSC no_trace_by_design
        (spec A2.3.3) and the remaining 5 are all not_recovered, so
        cases_recovered == 0
        WHEN the outcome is classified
        THEN it is INSUFFICIENT-COHORT, never REFUTED -- PSC rows are
        expected non-results by design and must never pad out a refutation's
        denominator, exactly like untestable rows."""
        outcome = classify_outcome(
            cases_recovered=0,
            cases_total=20,
            cases_untestable=0,
            cases_no_trace_by_design=15,
            precision=0.0,
            coverage_gate=_passing_coverage_gate(),
            stratum_gates=_all_passing_stratum_gates(),
        )
        assert outcome == "INSUFFICIENT-COHORT"

    def test_confirmed_still_reachable_at_exactly_the_pre_registered_cohort_size(self):
        """GIVEN exactly 20 testable cases (0 untestable, 0 no_trace_by_design)
        and every other threshold clearing
        WHEN the outcome is classified
        THEN it is CONFIRMED -- the cohort-size guard must not block a
        legitimate result at the exact pre-registered size."""
        outcome = classify_outcome(
            cases_recovered=4,
            cases_total=20,
            cases_untestable=0,
            cases_no_trace_by_design=0,
            precision=0.80,
            coverage_gate=_passing_coverage_gate(),
            stratum_gates=_all_passing_stratum_gates(),
        )
        assert outcome == "CONFIRMED"


class TestSplitRecoveredBySourceSeparation:
    """ADVERSARIAL FIX: a case recovered ONLY via a proven source-separation
    violation must never count as recovered (spec SS3)."""

    def _case(self, status: str, source_separation: str, key: str = "01234567") -> CaseEvaluation:
        return CaseEvaluation(
            case_key=key,
            company_number=key,
            row_count=1,
            award_count=1,
            earliest_award_date="2021-06-01",
            row_case_ids=["SYNTH-1"],
            status=status,
            source_separation=source_separation,
            row_evaluations=[],
        )

    def test_circular_recovered_case_is_excluded_from_clean(self):
        """GIVEN a recovered case whose source_separation is 'violation'
        (every path found is attested solely by an excluded source)
        WHEN recovered cases are split
        THEN it appears in `circular`, not `clean`."""
        case = self._case("recovered", "violation")
        clean, circular = split_recovered_by_source_separation([case])
        assert clean == []
        assert circular == [case]

    def test_ok_recovered_case_is_clean(self):
        """GIVEN a recovered case whose source_separation is 'ok' (an
        independent, permitted path exists)
        WHEN recovered cases are split
        THEN it appears in `clean`, not `circular`."""
        case = self._case("recovered", "ok")
        clean, circular = split_recovered_by_source_separation([case])
        assert clean == [case]
        assert circular == []

    def test_cannot_verify_recovered_case_is_clean_not_circular(self):
        """GIVEN a recovered case whose source_separation is 'cannot_verify'
        (an unattested edge -- nothing PROVES circularity)
        WHEN recovered cases are split
        THEN it appears in `clean`, not `circular` -- only a PROVEN
        violation is excluded, not an unresolved unknown."""
        case = self._case("recovered", "cannot_verify")
        clean, circular = split_recovered_by_source_separation([case])
        assert clean == [case]
        assert circular == []

    def test_non_recovered_cases_are_ignored(self):
        """GIVEN cases that are undated_only, not_recovered, or untestable
        WHEN recovered cases are split
        THEN none of them appear in either clean or circular -- only
        status == 'recovered' cases are considered at all."""
        cases = [
            self._case("undated_only", "violation", key="1"),
            self._case("not_recovered", "not_applicable", key="2"),
            self._case("untestable", "not_applicable", key="3"),
        ]
        clean, circular = split_recovered_by_source_separation(cases)
        assert clean == []
        assert circular == []

    def test_a_false_confirmed_via_circularity_is_impossible(self):
        """ADVERSARIAL TEST, end-to-end -- sneak past CONFIRMED via the
        project's own journalism ingest.

        GIVEN 4 cases all showing status='recovered' but source_separation
        == 'violation' on every one (check_source_separation has PROVEN the
        only path found for each is attested solely by that row's own
        excluded_from_retrieval source), with everything else passing
        (precision, retrieval controls, temporal gate, cohort size)
        WHEN the recovered cases are split and the clean count is fed into
        classify_outcome
        THEN the outcome is NOT CONFIRMED -- a case recoverable only through
        the project's own excluded/circular ingest must never produce an
        affirmative claim about a named person or company."""
        circular_cases = [self._case("recovered", "violation", key=str(i)) for i in range(4)]
        clean, circular = split_recovered_by_source_separation(circular_cases)
        assert len(circular) == 4
        assert len(clean) == 0

        outcome = classify_outcome(
            cases_recovered=len(clean),
            cases_total=20,
            cases_untestable=0,
            cases_no_trace_by_design=0,
            precision=1.0,
            coverage_gate=_passing_coverage_gate(),
            stratum_gates=_all_passing_stratum_gates(),
        )
        assert outcome != "CONFIRMED"

    def test_case_with_both_a_clean_and_a_tainted_path_is_clean_not_circular(self):
        """GIVEN evaluate_case's own roll-up logic (spec SS3): a case with
        one row landing 'ok' and another landing 'violation', both
        contributing to the winning 'recovered' status
        WHEN the case rollup's resulting source_separation is fed through the
        split
        THEN it is 'ok' (the clean path carries the case), so it lands in
        `clean` -- the distinction survives the case rollup, not just the
        per-row check."""
        # This mirrors evaluate_case's own aggregation rule directly:
        # "ok" in seps wins over "violation" being present too.
        case = self._case("recovered", "ok")
        clean, circular = split_recovered_by_source_separation([case])
        assert case in clean
        assert case not in circular


class TestCountrySwitchTriggered:
    """Spec A2.1.2: COUNTRY_SWITCH is an action, not a verdict."""

    @pytest.mark.parametrize("outcome", ["PARTIAL", "REFUTED", "INSTRUMENT-LIMITED"])
    def test_action_triggered_by_non_confirming_verdicts(self, outcome):
        """GIVEN a verdict of PARTIAL, REFUTED, or INSTRUMENT-LIMITED
        WHEN checking whether the country-switch action fires
        THEN it is triggered."""
        assert country_switch_triggered(outcome) is True

    @pytest.mark.parametrize("outcome", ["CONFIRMED", "INVALID", "INSUFFICIENT-COHORT"])
    def test_action_not_triggered_by_confirmed_invalid_or_insufficient_cohort(self, outcome):
        """GIVEN a verdict of CONFIRMED, INVALID, or INSUFFICIENT-COHORT
        WHEN checking whether the country-switch action fires
        THEN it is not triggered -- an inadequate cohort is fixed by
        sourcing more cases, not by switching country."""
        assert country_switch_triggered(outcome) is False


class TestComputePrecision:
    def test_precision_is_zero_over_zero_when_nothing_recovered(self):
        """GIVEN zero cases and zero negatives recovered
        WHEN precision is computed
        THEN it is 0.0, not a division error."""
        assert compute_precision(0, 0) == 0.0

    def test_precision_penalised_by_spurious_negative_hits(self):
        """GIVEN 4 true cases recovered and 1 spurious negative hit
        WHEN precision is computed
        THEN it is 4/5."""
        assert compute_precision(4, 1) == pytest.approx(0.8)


class TestWilsonUpperBound:
    """Spec A2.5: '0/200 is not a zero false-positive rate' -- must always
    carry an upper bound, never a bare zero."""

    def test_zero_successes_out_of_two_hundred_is_the_exact_wilson_value(self):
        """GIVEN zero spurious hits out of 200 trials
        WHEN the Wilson upper bound is computed
        THEN it is the exact ~1.88% Wilson score value, not merely 'small
        and positive' -- spec A2.5's whole point is that 0/200 does not mean
        a 0% rate, and the exact figure is what gets quoted in a result."""
        bound = wilson_upper_bound(0, 200)
        assert bound == pytest.approx(0.018845326377266575, abs=1e-9)

    def test_zero_trials_returns_zero(self):
        """GIVEN zero trials
        WHEN the Wilson upper bound is computed
        THEN it is 0.0, not a division error."""
        assert wilson_upper_bound(0, 0) == 0.0


@pytest.mark.django_db
class TestEvaluateRow:
    """The recovered / undated_only / untestable / not_recovered split, using
    the row's OWN award_date as cutoff."""

    def test_unresolved_supplier_is_untestable_not_refuted(self):
        """GIVEN a gold row whose company_number matches no graph entity
        WHEN the row is evaluated
        THEN its status is untestable, not not_recovered -- an unresolved
        endpoint must never be counted as evidence against H1."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        adj = build_adjacency()
        people_by_surname = {surname(person.name): [person]}

        result = evaluate_row(_gold_row(), adj, people_by_surname, {}, max_hops=2)

        assert result.status == "untestable"

    def test_preaward_path_is_recovered(self):
        """GIVEN a person and company connected by a single officer_of edge
        dated before the row's own award_date
        WHEN the row is evaluated
        THEN its status is recovered."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(
            entity_type="company", name="Example Holdings Ltd", company_number="01234567"
        )
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=person,
            target_entity=company,
            valid_from=date(2018, 1, 15),
        )
        adj = build_adjacency()
        people_by_surname = {surname(person.name): [person]}

        result = evaluate_row(_gold_row(), adj, people_by_surname, {}, max_hops=2)

        assert result.status == "recovered"

    def test_undated_path_is_undated_only_not_recovered_and_not_refuted(self):
        """GIVEN a person and company connected only by an edge with no
        valid_from (e.g. a declared_interest entry, spec SS7.2)
        WHEN the row is evaluated
        THEN its status is undated_only -- distinct from both recovered and
        not_recovered, exactly the distinction Phase C v1 failed to make."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(
            entity_type="company", name="Example Holdings Ltd", company_number="01234567"
        )
        Edge.objects.create(
            edge_type="declared_interest",
            source_entity=person,
            target_entity=company,
            valid_from=None,
        )
        adj = build_adjacency()
        people_by_surname = {surname(person.name): [person]}

        result = evaluate_row(_gold_row(), adj, people_by_surname, {}, max_hops=2)

        assert result.status == "undated_only"

    def test_no_path_at_all_is_not_recovered(self):
        """GIVEN a resolved person and company with no connecting edge at all
        WHEN the row is evaluated
        THEN its status is not_recovered -- a genuine miss, distinct from
        untestable (both endpoints DID resolve)."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        Entity.objects.create(
            entity_type="company", name="Example Holdings Ltd", company_number="01234567"
        )
        adj = build_adjacency()
        people_by_surname = {surname(person.name): [person]}

        result = evaluate_row(_gold_row(), adj, people_by_surname, {}, max_hops=2)

        assert result.status == "not_recovered"


@pytest.mark.django_db
class TestPscRelabeling:
    """Spec A2.3.3: a PSC-sourced row that finds no path is an expected
    no-trace, never a refutation -- but PSC never overrides a real result."""

    def test_psc_row_with_no_path_is_relabelled_no_trace_by_design(self):
        """GIVEN a PSC-sourced row whose person and company resolve but have
        no connecting path at all
        WHEN the row is evaluated
        THEN its status is no_trace_by_design, not not_recovered."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        Entity.objects.create(
            entity_type="company", name="Example Holdings Ltd", company_number="01234567"
        )
        adj = build_adjacency()
        people_by_surname = {surname(person.name): [person]}
        row = _gold_row(established_by="PSC")

        result = evaluate_row(row, adj, people_by_surname, {}, max_hops=2)

        assert result.status == "no_trace_by_design"

    def test_psc_row_that_recovers_via_independent_evidence_stays_recovered(self):
        """GIVEN a PSC-sourced row whose person and company ARE connected by
        an independently-ingested register edge dated before the award
        WHEN the row is evaluated
        THEN its status is recovered -- the PSC non-recovery expectation
        does not override a genuine recovery through other evidence."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(
            entity_type="company", name="Example Holdings Ltd", company_number="01234567"
        )
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=person,
            target_entity=company,
            valid_from=date(2018, 1, 15),
        )
        adj = build_adjacency()
        people_by_surname = {surname(person.name): [person]}
        row = _gold_row(established_by="PSC")

        result = evaluate_row(row, adj, people_by_surname, {}, max_hops=2)

        assert result.status == "recovered"

    def test_psc_row_that_is_untestable_stays_untestable(self):
        """GIVEN a PSC-sourced row whose company never resolves
        WHEN the row is evaluated
        THEN its status is untestable, not no_trace_by_design -- a
        resolution gap is a different, still-informative condition from the
        PSC temporal caveat."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        adj = build_adjacency()
        people_by_surname = {surname(person.name): [person]}
        row = _gold_row(established_by="PSC")

        result = evaluate_row(row, adj, people_by_surname, {}, max_hops=2)

        assert result.status == "untestable"


@pytest.mark.django_db
class TestEvaluateCase:
    """Spec A2.1.1/A2.3.2: a case rolls up ALL its rows into ONE status, using
    the case's EARLIEST qualifying award date as the cutoff for every row."""

    def test_case_recovers_if_any_row_recovers_even_if_others_are_untestable(self):
        """GIVEN a case with two rows -- one whose person/company resolve and
        recover a pre-award path, one whose person never resolves
        WHEN the case is evaluated
        THEN the case status is recovered -- one working row is enough for
        the whole case, exactly the Greensill-style scenario (five people,
        one company, one award) spec A2.1.1 describes."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(
            entity_type="company", name="Example Holdings Ltd", company_number="01234567"
        )
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=person,
            target_entity=company,
            valid_from=date(2018, 1, 15),
        )
        adj = build_adjacency()
        people_by_surname = {surname(person.name): [person]}

        recovering_row = _gold_row(case_id="SYNTH-200")
        unresolvable_row = _gold_row(
            case_id="SYNTH-201", person_name="Nobody Resolvable", person_registry_id=None
        )
        case = GoldCase(company_number="01234567", rows=(recovering_row, unresolvable_row))

        result = evaluate_case(case, adj, people_by_surname, {}, max_hops=2)

        assert result.status == "recovered"

    def test_case_status_is_the_strongest_among_its_rows(self):
        """GIVEN a case with one row that is undated_only and one that is
        not_recovered
        WHEN the case is evaluated
        THEN the case status is undated_only -- the stronger of the two,
        never downgraded to the weaker row's result."""
        person_a = Entity.objects.create(entity_type="person", name="Jane Testperson")
        person_b = Entity.objects.create(entity_type="person", name="Sam Otherperson")
        company = Entity.objects.create(
            entity_type="company", name="Example Holdings Ltd", company_number="01234567"
        )
        Edge.objects.create(
            edge_type="declared_interest",
            source_entity=person_a,
            target_entity=company,
            valid_from=None,
        )
        adj = build_adjacency()
        people_by_surname = {
            surname(person_a.name): [person_a],
            surname(person_b.name): [person_b],
        }

        undated_row = _gold_row(case_id="SYNTH-202", person_name="Jane Testperson")
        no_path_row = _gold_row(case_id="SYNTH-203", person_name="Sam Otherperson")
        case = GoldCase(company_number="01234567", rows=(undated_row, no_path_row))

        result = evaluate_case(case, adj, people_by_surname, {}, max_hops=2)

        assert result.status == "undated_only"

    def test_case_is_untestable_only_if_every_row_is_untestable(self):
        """GIVEN a case whose only row's person and company never resolve
        WHEN the case is evaluated
        THEN the case status is untestable."""
        adj = build_adjacency()
        case = GoldCase(company_number="01234567", rows=(_gold_row(),))

        result = evaluate_case(case, adj, {}, {}, max_hops=2)

        assert result.status == "untestable"

    def test_case_uses_earliest_award_date_not_each_rows_own(self):
        """GIVEN a case with two rows against the same awardee: one with an
        EARLIER award_date and no graph representation, one with a LATER
        award_date whose person has a path dated between the two award dates
        WHEN the case is evaluated
        THEN the later row's path is NOT counted as recovered -- spec A2.3.2
        requires testing against the case's earliest award date, not each
        row's own, even though the row would recover standalone against its
        own (later) award_date."""
        person_late = Entity.objects.create(entity_type="person", name="Sam Otherperson")
        company = Entity.objects.create(
            entity_type="company", name="Example Holdings Ltd", company_number="01234567"
        )
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=person_late,
            target_entity=company,
            valid_from=date(2021, 7, 1),
        )
        adj = build_adjacency()
        people_by_surname = {surname(person_late.name): [person_late]}

        early_row = _gold_row(
            case_id="SYNTH-210",
            person_name="Nobody Resolvable",
            award_date=date(2021, 6, 1),
        )
        late_row = _gold_row(
            case_id="SYNTH-211",
            person_name="Sam Otherperson",
            award_date=date(2021, 8, 15),
        )
        case = GoldCase(company_number="01234567", rows=(early_row, late_row))
        assert case.earliest_award_date == date(2021, 6, 1)

        # Standalone, against its OWN (later) award_date, this row recovers.
        standalone = evaluate_row(late_row, adj, people_by_surname, {}, max_hops=2)
        assert standalone.status == "recovered"

        result = evaluate_case(case, adj, people_by_surname, {}, max_hops=2)

        assert result.status == "undated_only"


class TestLoadCoverageGate:
    """Spec A2.4.2: the global coverage gate is measured by a separate
    process; this loader only consumes its result, always recomputing
    pass/fail from the underlying counts."""

    def test_missing_report_defaults_to_failing(self, tmp_path):
        """GIVEN no coverage gate report file exists at the given path
        WHEN it is loaded
        THEN the result is the all-False default -- never an implicit
        pass."""
        gate = load_coverage_gate(tmp_path / "does_not_exist.json")
        assert gate.passed is False

    def test_present_report_with_passing_counts_passes(self, tmp_path):
        """GIVEN a coverage gate report where both supplier-universe and
        Commons-universe counts clear the >=90% bar
        WHEN it is loaded
        THEN `passed` is True."""
        path = tmp_path / "coverage_gate.json"
        path.write_text(
            json.dumps(
                {
                    "supplier_universe_covered": 95,
                    "supplier_universe_total": 100,
                    "commons_universe_covered": 4057,
                    "commons_universe_total": 4057,
                }
            ),
            encoding="utf-8",
        )
        gate = load_coverage_gate(path)
        assert gate.passed is True

    def test_one_failing_universe_fails_the_whole_gate(self, tmp_path):
        """GIVEN supplier-universe coverage clears the bar but Commons
        universe coverage does not (only 50 of 4,057 records ingested)
        WHEN it is loaded
        THEN `passed` is False -- BOTH universe checks must pass (spec
        A2.4.2), one cannot compensate for the other."""
        path = tmp_path / "coverage_gate.json"
        path.write_text(
            json.dumps(
                {
                    "supplier_universe_covered": 100,
                    "supplier_universe_total": 100,
                    "commons_universe_covered": 50,
                    "commons_universe_total": 4057,
                }
            ),
            encoding="utf-8",
        )
        gate = load_coverage_gate(path)
        assert gate.passed is False


class TestLoadStratumGates:
    """Spec A2.4.3: per-material-stratum gates are measured by a separate
    control-battery process; this loader only consumes results, always
    recomputing pass/fail from the underlying counts and defaulting any
    missing stratum to unavailable."""

    def test_missing_report_defaults_every_stratum_to_unavailable(self, tmp_path):
        """GIVEN no stratum gates report file exists
        WHEN it is loaded
        THEN every material stratum defaults to an unavailable
        (never-passing) gate."""
        gates = load_stratum_gates(tmp_path / "does_not_exist.json")
        assert set(gates) == set(MATERIAL_STRATA)
        assert all(not g.passed for g in gates.values())

    def test_missing_stratum_entry_defaults_to_unavailable(self, tmp_path):
        """GIVEN a report that only supplies Commons and CH-officer entries
        (Lords omitted -- spec A2.4.2: 'Lords source coverage -- gating --
        currently unavailable')
        WHEN it is loaded
        THEN the Lords stratum defaults to an unavailable gate, never
        silently passing by omission."""
        path = tmp_path / "stratum_gates.json"
        path.write_text(
            json.dumps(
                {
                    STRATUM_COMMONS: {
                        "available": True,
                        "retrieval_recovered": 9,
                        "retrieval_total": 10,
                        "temporal_recovered": 9,
                        "temporal_total": 10,
                    },
                    STRATUM_CH_OFFICER: {
                        "available": True,
                        "retrieval_recovered": 10,
                        "retrieval_total": 10,
                        "temporal_recovered": 10,
                        "temporal_total": 10,
                    },
                }
            ),
            encoding="utf-8",
        )
        gates = load_stratum_gates(path)
        assert gates[STRATUM_LORDS].available is False
        assert gates[STRATUM_LORDS].passed is False
        assert gates[STRATUM_COMMONS].passed is True

    def test_claimed_available_true_is_not_trusted_without_supporting_numbers(self, tmp_path):
        """ADVERSARIAL TEST -- a stratum entry claims available:true but its
        own retrieval/temporal numbers (3/10) do not clear the bar.

        GIVEN a stratum marked available with counts well below 90%
        WHEN it is loaded
        THEN `passed` is False -- pass/fail is always recomputed from the
        numbers, never a trusted flag."""
        path = tmp_path / "stratum_gates.json"
        path.write_text(
            json.dumps(
                {
                    STRATUM_COMMONS: {
                        "available": True,
                        "retrieval_recovered": 3,
                        "retrieval_total": 10,
                        "temporal_recovered": 3,
                        "temporal_total": 10,
                    },
                }
            ),
            encoding="utf-8",
        )
        gates = load_stratum_gates(path)
        assert gates[STRATUM_COMMONS].passed is False

    def test_available_but_only_retrieval_passing_is_not_a_full_pass(self, tmp_path):
        """GIVEN a stratum whose retrieval numbers clear the bar but whose
        temporal numbers do not
        WHEN it is loaded
        THEN `passed` is False -- both retrieval AND temporal must pass."""
        path = tmp_path / "stratum_gates.json"
        path.write_text(
            json.dumps(
                {
                    STRATUM_CH_OFFICER: {
                        "available": True,
                        "retrieval_recovered": 10,
                        "retrieval_total": 10,
                        "temporal_recovered": 2,
                        "temporal_total": 10,
                    },
                }
            ),
            encoding="utf-8",
        )
        gates = load_stratum_gates(path)
        assert gates[STRATUM_CH_OFFICER].retrieval_passed is True
        assert gates[STRATUM_CH_OFFICER].temporal_passed is False
        assert gates[STRATUM_CH_OFFICER].passed is False


@pytest.mark.django_db
class TestClassifyEdgeStratum:
    """Spec A2.4.3: mapping a path edge to a material stratum. Commons and
    Lords `declared_interest` edges share the same entity registry_scheme,
    so only the attesting source_name distinguishes them."""

    def test_officer_of_edge_is_ch_officer_stratum(self):
        """GIVEN an officer_of edge
        WHEN its stratum is classified
        THEN it is the CH officer/appointment stratum."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(entity_type="company", name="Example Ltd")
        edge = Edge.objects.create(
            edge_type="officer_of", source_entity=person, target_entity=company
        )
        assert classify_edge_stratum(edge) == STRATUM_CH_OFFICER

    def test_declared_interest_attested_by_commons_is_commons_stratum(self):
        """GIVEN a declared_interest edge attested by the Commons register
        WHEN its stratum is classified
        THEN it is the Commons declared_interest stratum."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(entity_type="company", name="Example Ltd")
        edge = Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=company
        )
        Attestation.objects.create(edge=edge, source_name="UK Parliament Register of Interests")
        assert classify_edge_stratum(edge) == STRATUM_COMMONS

    def test_declared_interest_attested_by_lords_is_lords_stratum(self):
        """GIVEN a declared_interest edge attested by the Lords register
        (the SAME edge_type and entity registry_scheme as a Commons entry)
        WHEN its stratum is classified
        THEN it is the Lords declared_interest stratum -- distinguished only
        by the attesting source_name."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(entity_type="company", name="Example Ltd")
        edge = Edge.objects.create(
            edge_type="declared_interest", source_entity=person, target_entity=company
        )
        Attestation.objects.create(edge=edge, source_name="UK House of Lords Register of Interests")
        assert classify_edge_stratum(edge) == STRATUM_LORDS

    def test_donation_edge_has_no_material_stratum(self):
        """GIVEN an edge type that is not one of the three material strata
        WHEN its stratum is classified
        THEN it is None."""
        company = Entity.objects.create(entity_type="company", name="Example Ltd")
        party = Entity.objects.create(entity_type="political_party", name="Party A")
        edge = Edge.objects.create(edge_type="donation", source_entity=company, target_entity=party)
        assert classify_edge_stratum(edge) is None


@pytest.mark.django_db
class TestPathStrata:
    """A path's stratum set is the union of its edges' material strata."""

    def test_single_edge_path_has_one_stratum(self):
        """GIVEN a one-edge path via officer_of
        WHEN its strata are computed
        THEN the set contains exactly the CH-officer stratum."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(entity_type="company", name="Example Ltd")
        edge = Edge.objects.create(
            edge_type="officer_of", source_entity=person, target_entity=company
        )
        assert path_strata([edge]) == frozenset({STRATUM_CH_OFFICER})

    def test_two_edge_path_unions_both_strata(self):
        """GIVEN a two-edge path: one officer_of edge and one Commons
        declared_interest edge
        WHEN its strata are computed
        THEN the set contains BOTH material strata."""
        person_a = Entity.objects.create(entity_type="person", name="Jane Testperson")
        person_b = Entity.objects.create(entity_type="person", name="Sam Otherperson")
        company = Entity.objects.create(entity_type="company", name="Example Ltd")
        edge_officer = Edge.objects.create(
            edge_type="officer_of", source_entity=person_a, target_entity=company
        )
        edge_commons = Edge.objects.create(
            edge_type="declared_interest", source_entity=person_a, target_entity=person_b
        )
        Attestation.objects.create(
            edge=edge_commons, source_name="UK Parliament Register of Interests"
        )
        assert path_strata([edge_officer, edge_commons]) == frozenset(
            {STRATUM_CH_OFFICER, STRATUM_COMMONS}
        )

    def test_same_as_edge_contributes_no_stratum(self):
        """GIVEN a path containing a same_as identity-bridge edge alongside
        a material one
        WHEN its strata are computed
        THEN only the material edge's stratum appears -- same_as is not
        evidence of a relationship."""
        person_a = Entity.objects.create(entity_type="person", name="Jane Testperson")
        person_b = Entity.objects.create(entity_type="person", name="Jane Testperson (CH)")
        company = Entity.objects.create(entity_type="company", name="Example Ltd")
        edge_same_as = Edge.objects.create(
            edge_type="same_as", source_entity=person_a, target_entity=person_b
        )
        edge_officer = Edge.objects.create(
            edge_type="officer_of", source_entity=person_b, target_entity=company
        )
        assert path_strata([edge_same_as, edge_officer]) == frozenset({STRATUM_CH_OFFICER})


class TestFilterByPassingStratum:
    """Spec A2.4.4: a recovered case must belong to at least one PASSING
    stratum to count toward CONFIRMED/PARTIAL/REFUTED."""

    def _case(self, strata: frozenset[str], key: str = "01234567") -> CaseEvaluation:
        return CaseEvaluation(
            case_key=key,
            company_number=key,
            row_count=1,
            award_count=1,
            earliest_award_date="2021-06-01",
            row_case_ids=["SYNTH-1"],
            status="recovered",
            source_separation="ok",
            row_evaluations=[],
            strata=strata,
        )

    def test_case_touching_a_passing_stratum_qualifies(self):
        """GIVEN a case whose evidence touches the Commons stratum, which
        passes
        WHEN cases are filtered by passing stratum
        THEN it is qualifying, not instrument-limited."""
        case = self._case(frozenset({STRATUM_COMMONS}))
        gates = _all_passing_stratum_gates()
        qualifying, instrument_limited = filter_by_passing_stratum([case], gates)
        assert qualifying == [case]
        assert instrument_limited == []

    def test_case_touching_only_an_unsupported_stratum_is_instrument_limited(self):
        """ADVERSARIAL TEST -- a case recovered ONLY through Lords evidence
        while Lords remains unavailable.

        GIVEN a case whose ONLY evidence is the Lords stratum, and Lords is
        unavailable
        WHEN cases are filtered by passing stratum
        THEN it is instrument-limited, NOT qualifying -- it must never
        count toward CONFIRMED/PARTIAL/REFUTED (spec A2.4.4)."""
        case = self._case(frozenset({STRATUM_LORDS}))
        gates = _all_passing_stratum_gates()
        gates[STRATUM_LORDS] = StratumGate(available=False)
        qualifying, instrument_limited = filter_by_passing_stratum([case], gates)
        assert qualifying == []
        assert instrument_limited == [case]

    def test_case_touching_both_a_passing_and_unsupported_stratum_qualifies(self):
        """GIVEN a case whose evidence touches BOTH Commons (passing) and
        Lords (unavailable)
        WHEN cases are filtered by passing stratum
        THEN it qualifies -- spec A2.4.4: an unvalidated Lords gate must not
        erase a genuine, independently verified Commons recovery."""
        case = self._case(frozenset({STRATUM_COMMONS, STRATUM_LORDS}))
        gates = _all_passing_stratum_gates()
        gates[STRATUM_LORDS] = StratumGate(available=False)
        qualifying, instrument_limited = filter_by_passing_stratum([case], gates)
        assert qualifying == [case]
        assert instrument_limited == []


@pytest.mark.django_db
class TestSourceSeparation:
    """Spec SS3: excluded_from_retrieval sources must not make a row recoverable."""

    def test_no_excluded_sources_is_not_applicable(self):
        """GIVEN a row with no excluded_from_retrieval sources listed
        WHEN source separation is checked
        THEN the result is not_applicable -- there is nothing to separate."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(entity_type="company", name="Example Ltd")
        edge = Edge.objects.create(
            edge_type="officer_of", source_entity=person, target_entity=company
        )

        assert check_source_separation([[edge]], ()) == "not_applicable"

    def test_attestation_matching_excluded_source_is_a_violation(self):
        """GIVEN every found path's only attestation names a source in the
        row's excluded_from_retrieval list
        WHEN source separation is checked
        THEN it is reported as a violation."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(entity_type="company", name="Example Ltd")
        edge = Edge.objects.create(
            edge_type="officer_of", source_entity=person, target_entity=company
        )
        Attestation.objects.create(edge=edge, source_name="The Guardian investigation")

        result = check_source_separation([[edge]], ("Guardian investigation",))

        assert result == "violation"

    def test_attestation_from_permitted_source_is_ok(self):
        """GIVEN the found path's attestation names a register source not in
        the excluded list
        WHEN source separation is checked
        THEN it is reported as ok."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(entity_type="company", name="Example Ltd")
        edge = Edge.objects.create(
            edge_type="officer_of", source_entity=person, target_entity=company
        )
        Attestation.objects.create(edge=edge, source_name="Companies House")

        result = check_source_separation([[edge]], ("Guardian investigation",))

        assert result == "ok"

    def test_unattested_edge_cannot_be_verified(self):
        """GIVEN the only found path's edge has zero attestations
        WHEN source separation is checked
        THEN the result is cannot_verify -- absence of a match is not proof
        of separation when provenance was never recorded."""
        person = Entity.objects.create(entity_type="person", name="Jane Testperson")
        company = Entity.objects.create(entity_type="company", name="Example Ltd")
        edge = Edge.objects.create(
            edge_type="officer_of", source_entity=person, target_entity=company
        )

        result = check_source_separation([[edge]], ("Guardian investigation",))

        assert result == "cannot_verify"
