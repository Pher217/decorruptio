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
    TemporalGate,
    check_source_separation,
    classify_outcome,
    compute_precision,
    country_switch_triggered,
    evaluate_case,
    evaluate_row,
    load_temporal_gate,
    wilson_upper_bound,
)

from uncorrupt.graph.models import Attestation, Edge, Entity

MANIFEST_HEADER = (
    "case_id,person_name,person_registry_id,company_name,company_number,"
    "relationship_type,established_by,label_source_url,award_date,"
    "relationship_start,excluded_from_retrieval,intermediary_company_number,"
    "awardee_confirmed\n"
)


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
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,yes"
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
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,yes"
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
                "journalism,https://example.invalid/a,,2018-01-15,,,yes"
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
                "journalism,https://example.invalid/a,2019-01-01,2020-05-01,,,yes"
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
                "journalism,https://example.invalid/a,2021-06-01,unknown,,,yes"
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
                "journalism,,2021-06-01,2018-01-15,,,yes"
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
                "https://example.invalid/a,2021-06-01,2018-01-15,,,yes",
                "SYNTH-007,B,,Other Ltd,7654321,directorship,journalism,"
                "https://example.invalid/b,2021-06-01,2018-01-15,,,yes",
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
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,no"
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
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,"
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
                "journalism,https://example.invalid/a,2021-09-01,2019-11-01,,999888,yes"
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
                "PSC,https://example.invalid/a,2021-01-10,2019-03-01,,,yes"
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
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,yes",
                "SYNTH-011,Person Two,,Example Holdings Ltd,1234567,shareholding,"
                "inquiry,https://example.invalid/b,2021-08-15,2019-02-20,,,yes",
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
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,,,yes",
                "SYNTH-013,Person Two,,Other Traders Ltd,7654321,directorship,"
                "inquiry,https://example.invalid/b,2021-06-01,2019-02-20,,,yes",
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.cases) == 2
        assert all(not c.is_concentrated for c in result.cases)


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


class TestClassifyOutcome:
    """Spec A2.1.2 (amendment v2.1) LOCKED verdict set, case-level."""

    PASSING_GATE = TemporalGate(passed=True, overall_recovered=28, overall_total=30)
    FAILING_GATE = TemporalGate(passed=False, overall_recovered=13, overall_total=30)

    def test_invalid_fires_below_nine_of_ten_retrieval_controls(self):
        """GIVEN retrieval controls recovered only 8 out of 10
        WHEN the outcome is classified
        THEN it is INVALID regardless of cases recovered or the temporal
        gate -- spec: the positives result 'must not be reported'."""
        outcome = classify_outcome(
            cases_recovered=20,
            precision=1.0,
            retrieval_controls_recovered=8,
            retrieval_controls_total=10,
            temporal_gate=self.PASSING_GATE,
        )
        assert outcome == "INVALID"

    def test_instrument_limited_fires_when_temporal_gate_fails(self):
        """GIVEN retrieval controls pass but the temporal control gate fails
        WHEN the outcome is classified
        THEN it is INSTRUMENT-LIMITED, never REFUTED (spec A2.3)."""
        outcome = classify_outcome(
            cases_recovered=0,
            precision=0.0,
            retrieval_controls_recovered=10,
            retrieval_controls_total=10,
            temporal_gate=self.FAILING_GATE,
        )
        assert outcome == "INSTRUMENT-LIMITED"

    def test_instrument_limited_fires_when_temporal_gate_not_measured(self):
        """GIVEN no temporal control gate has been measured yet (None)
        WHEN the outcome is classified
        THEN it is INSTRUMENT-LIMITED -- an unmeasured gate is never an
        implicit pass, so REFUTED can never be emitted without one."""
        outcome = classify_outcome(
            cases_recovered=0,
            precision=0.0,
            retrieval_controls_recovered=10,
            retrieval_controls_total=10,
            temporal_gate=None,
        )
        assert outcome == "INSTRUMENT-LIMITED"

    def test_confirmed_fires_at_locked_thresholds_with_temporal_gate_passing(self):
        """GIVEN >=4/20 cases recovered, >=80% precision, retrieval controls
        passing, AND the temporal gate passing
        WHEN the outcome is classified
        THEN it is CONFIRMED."""
        outcome = classify_outcome(
            cases_recovered=4,
            precision=0.80,
            retrieval_controls_recovered=9,
            retrieval_controls_total=10,
            temporal_gate=self.PASSING_GATE,
        )
        assert outcome == "CONFIRMED"

    def test_confirmed_is_blocked_by_a_failing_temporal_gate_even_at_full_thresholds(self):
        """GIVEN cases recovered and precision both clear the CONFIRMED bar
        but the temporal gate fails
        WHEN the outcome is classified
        THEN it is INSTRUMENT-LIMITED, not CONFIRMED -- the temporal gate is
        a hard requirement, not merely advisory."""
        outcome = classify_outcome(
            cases_recovered=10,
            precision=1.0,
            retrieval_controls_recovered=10,
            retrieval_controls_total=10,
            temporal_gate=self.FAILING_GATE,
        )
        assert outcome == "INSTRUMENT-LIMITED"

    def test_refuted_fires_at_zero_cases_with_both_gates_passing(self):
        """GIVEN 0/20 cases recovered while retrieval controls AND the
        temporal gate both pass
        WHEN the outcome is classified
        THEN it is REFUTED."""
        outcome = classify_outcome(
            cases_recovered=0,
            precision=0.0,
            retrieval_controls_recovered=10,
            retrieval_controls_total=10,
            temporal_gate=self.PASSING_GATE,
        )
        assert outcome == "REFUTED"

    def test_partial_fires_for_one_to_three_cases_with_both_gates_passing(self):
        """GIVEN both gates pass but cases recovered is short of the
        CONFIRMED threshold without being exactly zero
        WHEN the outcome is classified
        THEN it is PARTIAL -- real traces below the pre-registered
        confirmation bar, never rendered as a confirmation."""
        outcome = classify_outcome(
            cases_recovered=2,
            precision=1.0,
            retrieval_controls_recovered=10,
            retrieval_controls_total=10,
            temporal_gate=self.PASSING_GATE,
        )
        assert outcome == "PARTIAL"


class TestCountrySwitchTriggered:
    """Spec A2.1.2: COUNTRY_SWITCH is an action, not a verdict."""

    @pytest.mark.parametrize("outcome", ["PARTIAL", "REFUTED", "INSTRUMENT-LIMITED"])
    def test_action_triggered_by_non_confirming_verdicts(self, outcome):
        """GIVEN a verdict of PARTIAL, REFUTED, or INSTRUMENT-LIMITED
        WHEN checking whether the country-switch action fires
        THEN it is triggered."""
        assert country_switch_triggered(outcome) is True

    @pytest.mark.parametrize("outcome", ["CONFIRMED", "INVALID"])
    def test_action_not_triggered_by_confirmed_or_invalid(self, outcome):
        """GIVEN a verdict of CONFIRMED or INVALID
        WHEN checking whether the country-switch action fires
        THEN it is not triggered."""
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

    def test_zero_successes_has_a_small_positive_upper_bound(self):
        """GIVEN zero spurious hits out of 200 trials
        WHEN the Wilson upper bound is computed
        THEN it is a small positive number, not zero -- spec A2.5's whole
        point is that 0/200 does not mean a 0% rate."""
        bound = wilson_upper_bound(0, 200)
        assert 0.0 < bound < 0.05

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


class TestLoadTemporalGate:
    """Spec A2.3: the temporal control gate is measured by a separate
    classifier; this loader only consumes its result."""

    def test_missing_report_returns_none(self, tmp_path):
        """GIVEN no temporal gate report file exists at the given path
        WHEN it is loaded
        THEN None is returned -- treated by classify_outcome as a failing
        gate, never an implicit pass."""
        assert load_temporal_gate(tmp_path / "does_not_exist.json") is None

    def test_present_report_is_parsed(self, tmp_path):
        """GIVEN a temporal gate report file written by the (separate)
        temporal-lift classifier
        WHEN it is loaded
        THEN its passed/recovered/total/failing_strata fields are parsed
        into a TemporalGate."""
        path = tmp_path / "temporal_gate.json"
        path.write_text(
            json.dumps(
                {
                    "passed": False,
                    "overall_recovered": 13,
                    "overall_total": 30,
                    "failing_strata": ["declared_interest/Lords"],
                }
            ),
            encoding="utf-8",
        )

        gate = load_temporal_gate(path)

        assert gate == TemporalGate(
            passed=False,
            overall_recovered=13,
            overall_total=30,
            failing_strata=("declared_interest/Lords",),
        )


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
