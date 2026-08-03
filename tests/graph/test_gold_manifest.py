"""Tests for the Phase C gold-manifest loader and pre-registered benchmark runner.

Spec (LOCKED): vault
`02 Projects/Ideas/Decorruptio/05 Specs/phase-c-gold-manifest-preregistration.md`.

These tests exist because Phase C v1 conflated outcomes that are genuinely
different -- treating "path found" as proof of truth, and "endpoint never
resolved" as a refutation -- and the project had to retract a result because
of it. Every test below asserts one of those specific failure modes cannot
slip through here unnoticed.

All manifest data here is synthetic (SYNTH-* case IDs, example.invalid URLs,
placeholder names) -- never a real person, company, or case.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from scripts.load_gold_manifest import GoldRow, load_gold_manifest
from scripts.phase_c_paths import build_adjacency, surname
from scripts.run_gold_benchmark import (
    check_source_separation,
    classify_outcome,
    compute_precision,
    evaluate_row,
)

from uncorrupt.graph.models import Attestation, Edge, Entity

MANIFEST_HEADER = (
    "case_id,person_name,person_registry_id,company_name,company_number,"
    "relationship_type,established_by,label_source_url,award_date,"
    "relationship_start,excluded_from_retrieval\n"
)


def _write_manifest(tmp_path: Path, rows: list[str]) -> Path:
    path = tmp_path / "manifest.csv"
    path.write_text(MANIFEST_HEADER + "\n".join(rows) + "\n", encoding="utf-8")
    return path


class TestLoadGoldManifest:
    """Loader admissibility (spec SS2) and schema (spec SS4) checks."""

    def test_admissible_row_is_loaded_and_company_number_normalised(self, tmp_path):
        """GIVEN a row that satisfies every spec SS2 admissibility criterion
        WHEN the manifest is loaded
        THEN it is admissible and its company number is zero-padded to 8
        characters by the project's canonical CH normaliser."""
        path = _write_manifest(
            tmp_path,
            [
                "SYNTH-001,Jane Testperson,,Example Holdings Ltd,1234567,directorship,"
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,"
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
                "journalism,https://example.invalid/a,2021-06-01,2018-01-15,"
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
                "journalism,https://example.invalid/a,,2018-01-15,"
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
                "journalism,https://example.invalid/a,2019-01-01,2020-05-01,"
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
                "journalism,https://example.invalid/a,2021-06-01,unknown,"
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
                "journalism,,2021-06-01,2018-01-15,"
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
                "https://example.invalid/a,2021-06-01,2018-01-15,",
                "SYNTH-007,B,,Other Ltd,7654321,directorship,journalism,"
                "https://example.invalid/b,2021-06-01,2018-01-15,",
            ],
        )
        result = load_gold_manifest(path)
        assert len(result.admissible) == 1
        assert any("duplicate case_id" in r for r in result.inadmissible[0].reasons)


class TestClassifyOutcome:
    """Spec SS6 LOCKED acceptance thresholds."""

    def test_invalid_fires_below_nine_of_ten_controls(self):
        """GIVEN controls recovered only 8 out of 10
        WHEN the outcome is classified
        THEN it is INVALID regardless of how many positives were recovered --
        spec SS6: the positives result 'must not be reported'."""
        outcome = classify_outcome(
            positives_recovered=20, precision=1.0, controls_recovered=8, controls_total=10
        )
        assert outcome == "INVALID"

    def test_confirmed_fires_at_locked_thresholds(self):
        """GIVEN >=4/20 positives recovered, >=80% precision, and controls passing
        WHEN the outcome is classified
        THEN it is CONFIRMED."""
        outcome = classify_outcome(
            positives_recovered=4, precision=0.80, controls_recovered=9, controls_total=10
        )
        assert outcome == "CONFIRMED"

    def test_refuted_fires_at_zero_recovered_with_controls_passing(self):
        """GIVEN 0/20 positives recovered while controls pass
        WHEN the outcome is classified
        THEN it is REFUTED."""
        outcome = classify_outcome(
            positives_recovered=0, precision=0.0, controls_recovered=10, controls_total=10
        )
        assert outcome == "REFUTED"

    def test_country_switch_fires_for_a_short_but_nonzero_recovery(self):
        """GIVEN controls pass but positives recovered is short of the
        CONFIRMED threshold without being exactly zero
        WHEN the outcome is classified
        THEN it is COUNTRY_SWITCH -- REFUTED is reserved for the stronger
        0/20 statistical bound."""
        outcome = classify_outcome(
            positives_recovered=2, precision=1.0, controls_recovered=10, controls_total=10
        )
        assert outcome == "COUNTRY_SWITCH"


class TestComputePrecision:
    def test_precision_is_zero_over_zero_when_nothing_recovered(self):
        """GIVEN zero positives and zero negatives recovered
        WHEN precision is computed
        THEN it is 0.0, not a division error."""
        assert compute_precision(0, 0) == 0.0

    def test_precision_penalised_by_spurious_negative_hits(self):
        """GIVEN 4 true positives recovered and 1 spurious negative hit
        WHEN precision is computed
        THEN it is 4/5."""
        assert compute_precision(4, 1) == pytest.approx(0.8)


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
    )
    defaults.update(overrides)
    return GoldRow(**defaults)


@pytest.mark.django_db
class TestEvaluateRow:
    """The three-way (recovered / undated_only / untestable) + not_recovered split."""

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
