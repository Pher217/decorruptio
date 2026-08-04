"""Tests for the Companies House-stratum external control runner.

`scripts/run_ch_controls.py` closes the per-stratum control-battery gap
required by amendment v2.4 for Companies House: every control is a real
(officer_id, company_number, appointed_on) triple verified live against the
CH REST API (see `tests/fixtures/ch_temporal_controls.json`'s own `source`
block), never sampled from our graph.

These tests verify the discipline the packet requires:
- a present officer_of relationship is recovered
- an absent officer/company is classified `not-found`, never treated as a
  retrieval-logic failure
- retrieval and temporal are reported as genuinely distinct figures -- a
  retrieved relationship whose edge date does not match the externally
  verified appointed_on is temporal `date_mismatch_or_missing`, not a
  silent pass
- selection from the fixture is deterministic across repeated loads/runs
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from scripts.run_ch_controls import (
    DEFAULT_CONTROLS_PATH,
    STATUS_NOT_FOUND,
    STATUS_RECOVERED,
    TEMPORAL_MATCHED,
    TEMPORAL_MISMATCH,
    TEMPORAL_NOT_APPLICABLE,
    classify_control,
    load_controls,
    run_controls,
)

from uncorrupt.graph.models import Edge, Entity


def _write_controls(tmp_path: Path, controls: list[dict]) -> Path:
    path = tmp_path / "controls.json"
    path.write_text(json.dumps({"controls": controls}), encoding="utf-8")
    return path


class TestLoadControlsIsDeterministic:
    def test_load_controls_returns_the_full_fixed_battery(self):
        """GIVEN the pre-registered 12-row control fixture
        WHEN it is loaded
        THEN all 12 rows are returned, in file order."""
        controls = load_controls(DEFAULT_CONTROLS_PATH)
        assert len(controls) == 12
        assert [c["id"] for c in controls] == list(range(1, 13))

    def test_load_controls_is_deterministic_across_repeated_calls(self):
        """GIVEN the same fixture path
        WHEN loaded twice
        THEN the ordered id sequence is identical both times -- nothing here
        samples, shuffles, or reselects to flatter a result."""
        first = load_controls(DEFAULT_CONTROLS_PATH)
        second = load_controls(DEFAULT_CONTROLS_PATH)
        assert [c["id"] for c in first] == [c["id"] for c in second]


@pytest.mark.django_db
class TestClassifyControlRecovered:
    def test_present_appointment_is_recovered_with_matching_temporal(self):
        """GIVEN an officer_of edge whose valid_from matches the externally
        verified appointed_on
        WHEN the control is classified
        THEN status is 'recovered' AND temporal_status is 'matched'."""
        officer = Entity.objects.create(
            entity_type="person",
            name="BOWDEN, Matthew Shaun",
            registry_scheme="GB-COH-OFFICER",
            registry_id="hfUVbB1SOxpAmXJj443yaiug7Lk",
        )
        company = Entity.objects.create(
            entity_type="company",
            name="ASTRAZENECA PLC",
            registry_scheme="GB-COH",
            registry_id="02723534",
            company_number="02723534",
        )
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=officer,
            target_entity=company,
            valid_from=date(2025, 5, 1),
        )
        control = {
            "id": 1,
            "query": "ASTRAZENECA",
            "officer_id": "hfUVbB1SOxpAmXJj443yaiug7Lk",
            "officer_name": "BOWDEN, Matthew Shaun",
            "company_number": "02723534",
            "company_name": "ASTRAZENECA PLC",
            "appointed_on": "2025-05-01",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_RECOVERED
        assert row["temporal_status"] == TEMPORAL_MATCHED

    def test_recovered_relationship_with_wrong_date_is_temporal_mismatch(self):
        """GIVEN an officer_of edge that connects the same officer and company
        but with a DIFFERENT valid_from than the externally verified
        appointed_on
        WHEN classified
        THEN status is still 'recovered' (the relationship exists) but
        temporal_status is 'date_mismatch_or_missing' -- retrieval and
        temporal are never conflated."""
        officer = Entity.objects.create(
            entity_type="person",
            name="BOWDEN, Matthew Shaun",
            registry_scheme="GB-COH-OFFICER",
            registry_id="hfUVbB1SOxpAmXJj443yaiug7Lk",
        )
        company = Entity.objects.create(
            entity_type="company",
            name="ASTRAZENECA PLC",
            registry_scheme="GB-COH",
            registry_id="02723534",
            company_number="02723534",
        )
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=officer,
            target_entity=company,
            valid_from=date(2019, 1, 1),
        )
        control = {
            "id": 1,
            "officer_id": "hfUVbB1SOxpAmXJj443yaiug7Lk",
            "company_number": "02723534",
            "appointed_on": "2025-05-01",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_RECOVERED
        assert row["temporal_status"] == TEMPORAL_MISMATCH

    def test_recovered_relationship_with_no_edge_date_is_temporal_mismatch(self):
        """GIVEN an officer_of edge with a null valid_from
        WHEN classified
        THEN status is 'recovered' but temporal_status is
        'date_mismatch_or_missing' -- a missing date is never silently
        treated as a temporal pass."""
        officer = Entity.objects.create(
            entity_type="person",
            name="BOWDEN, Matthew Shaun",
            registry_scheme="GB-COH-OFFICER",
            registry_id="hfUVbB1SOxpAmXJj443yaiug7Lk",
        )
        company = Entity.objects.create(
            entity_type="company",
            name="ASTRAZENECA PLC",
            registry_scheme="GB-COH",
            registry_id="02723534",
            company_number="02723534",
        )
        Edge.objects.create(edge_type="officer_of", source_entity=officer, target_entity=company)
        control = {
            "id": 1,
            "officer_id": "hfUVbB1SOxpAmXJj443yaiug7Lk",
            "company_number": "02723534",
            "appointed_on": "2025-05-01",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_RECOVERED
        assert row["temporal_status"] == TEMPORAL_MISMATCH


@pytest.mark.django_db
class TestClassifyControlNotFound:
    def test_absent_officer_is_not_found_with_no_applicable_temporal(self):
        """GIVEN a company that exists but no GB-COH-OFFICER entity for the
        officer at all
        WHEN classified
        THEN status is 'not-found' and temporal_status is 'not_applicable'
        -- absence is a data-coverage gap, never counted as a
        retrieval-logic failure."""
        Entity.objects.create(
            entity_type="company",
            name="ASTRAZENECA PLC",
            registry_scheme="GB-COH",
            registry_id="02723534",
            company_number="02723534",
        )
        control = {
            "id": 1,
            "officer_id": "nonexistent-officer-id",
            "company_number": "02723534",
            "appointed_on": "2025-05-01",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_NOT_FOUND
        assert row["temporal_status"] == TEMPORAL_NOT_APPLICABLE

    def test_absent_company_is_not_found(self):
        """GIVEN an officer that exists but no matching GB-COH company entity
        WHEN classified
        THEN status is 'not-found'."""
        Entity.objects.create(
            entity_type="person",
            name="BOWDEN, Matthew Shaun",
            registry_scheme="GB-COH-OFFICER",
            registry_id="hfUVbB1SOxpAmXJj443yaiug7Lk",
        )
        control = {
            "id": 1,
            "officer_id": "hfUVbB1SOxpAmXJj443yaiug7Lk",
            "company_number": "99999999",
            "appointed_on": "2025-05-01",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_NOT_FOUND

    def test_both_resolved_but_no_edge_is_not_found(self):
        """GIVEN an officer entity and a company entity that both exist but
        share no officer_of edge
        WHEN classified
        THEN status is 'not-found'."""
        Entity.objects.create(
            entity_type="person",
            name="BOWDEN, Matthew Shaun",
            registry_scheme="GB-COH-OFFICER",
            registry_id="hfUVbB1SOxpAmXJj443yaiug7Lk",
        )
        Entity.objects.create(
            entity_type="company",
            name="ASTRAZENECA PLC",
            registry_scheme="GB-COH",
            registry_id="02723534",
            company_number="02723534",
        )
        control = {
            "id": 1,
            "officer_id": "hfUVbB1SOxpAmXJj443yaiug7Lk",
            "company_number": "02723534",
            "appointed_on": "2025-05-01",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_NOT_FOUND


@pytest.mark.django_db
class TestRunControlsDeterminism:
    def test_run_controls_is_deterministic_across_repeated_runs(self, tmp_path):
        """GIVEN a fixed control list and unchanged graph state
        WHEN run_controls executes twice in a row
        THEN both runs produce the identical per-row status sequence and
        summary counts."""
        officer = Entity.objects.create(
            entity_type="person",
            name="BOWDEN, Matthew Shaun",
            registry_scheme="GB-COH-OFFICER",
            registry_id="hfUVbB1SOxpAmXJj443yaiug7Lk",
        )
        company = Entity.objects.create(
            entity_type="company",
            name="ASTRAZENECA PLC",
            registry_scheme="GB-COH",
            registry_id="02723534",
            company_number="02723534",
        )
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=officer,
            target_entity=company,
            valid_from=date(2025, 5, 1),
        )
        controls = [
            {
                "id": 1,
                "officer_id": "hfUVbB1SOxpAmXJj443yaiug7Lk",
                "company_number": "02723534",
                "appointed_on": "2025-05-01",
            },
            {
                "id": 2,
                "officer_id": "nonexistent",
                "company_number": "00000000",
                "appointed_on": "2025-05-01",
            },
        ]
        path = _write_controls(tmp_path, controls)

        first = run_controls(path)
        second = run_controls(path)

        assert [r["status"] for r in first["rows"]] == [r["status"] for r in second["rows"]]
        assert (first["retrieval_recovered"], first["not_found"]) == (
            second["retrieval_recovered"],
            second["not_found"],
        )
        assert first["retrieval_recovered"] == 1
        assert first["not_found"] == 1


@pytest.mark.django_db
class TestRunControlsReportsRetrievalAndTemporalSeparately:
    def test_output_carries_both_figures_with_independent_denominators(self, tmp_path):
        """GIVEN a control battery of two rows: one fully recovered with a
        matching date, one recovered but with a mismatched date
        WHEN run_controls executes
        THEN retrieval_recovered counts both rows, temporal_recovered counts
        only the matching one -- retrieval and temporal are reported as
        genuinely distinct figures, never conflated into one."""
        officer1 = Entity.objects.create(
            entity_type="person", name="A", registry_scheme="GB-COH-OFFICER", registry_id="off-1"
        )
        company1 = Entity.objects.create(
            entity_type="company",
            name="Company One",
            registry_scheme="GB-COH",
            registry_id="00000001",
            company_number="00000001",
        )
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=officer1,
            target_entity=company1,
            valid_from=date(2020, 1, 1),
        )

        officer2 = Entity.objects.create(
            entity_type="person", name="B", registry_scheme="GB-COH-OFFICER", registry_id="off-2"
        )
        company2 = Entity.objects.create(
            entity_type="company",
            name="Company Two",
            registry_scheme="GB-COH",
            registry_id="00000002",
            company_number="00000002",
        )
        Edge.objects.create(
            edge_type="officer_of",
            source_entity=officer2,
            target_entity=company2,
            valid_from=date(2021, 6, 1),
        )

        controls = [
            {
                "id": 1,
                "officer_id": "off-1",
                "company_number": "00000001",
                "appointed_on": "2020-01-01",
            },
            {
                "id": 2,
                "officer_id": "off-2",
                "company_number": "00000002",
                "appointed_on": "1999-12-31",
            },
        ]
        path = _write_controls(tmp_path, controls)

        result = run_controls(path)

        assert result["retrieval_recovered"] == 2
        assert result["retrieval_total"] == 2
        assert result["temporal_recovered"] == 1
        assert result["temporal_total"] == 2


@pytest.mark.django_db
class TestRunControlsAgainstEmptyGraph:
    def test_full_fixture_against_empty_graph_is_all_not_found_and_does_not_crash(self):
        """GIVEN the real 12-row production fixture and an empty test graph
        WHEN run_controls executes
        THEN it completes without error and every row is 'not-found'."""
        result = run_controls(DEFAULT_CONTROLS_PATH)

        assert result["n"] == 12
        assert result["not_found"] == 12
        assert result["retrieval_recovered"] == 0
        assert result["temporal_recovered"] == 0
