"""Tests for the Electoral Commission-stratum external control runner.

`scripts/run_ec_controls.py` closes the per-stratum control-battery gap for
Electoral Commission donations: every control is a real (donor company,
recipient, acceptedDate/receivedDate, donor company registration number)
fact verified live against the EC's public donation search CSV export (see
`tests/fixtures/ec_retrieval_controls.json`'s own `source` block), never
sampled from our graph.

These tests verify the discipline the packet requires:
- a present donation relationship is recovered
- an absent donor/recipient is classified `not-found`, never counted as a
  retrieval-logic failure
- retrieval and temporal are reported as genuinely distinct figures --
  including that temporal correctly compares against `ReceivedDate`, not
  bare `AcceptedDate`, mirroring `ec_donations.py`'s own valid_from choice
- selection from the fixture is deterministic across repeated loads/runs
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from scripts.run_ec_controls import (
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
        THEN the ordered id sequence is identical both times."""
        first = load_controls(DEFAULT_CONTROLS_PATH)
        second = load_controls(DEFAULT_CONTROLS_PATH)
        assert [c["id"] for c in first] == [c["id"] for c in second]


@pytest.mark.django_db
class TestClassifyControlRecovered:
    def test_present_donation_is_recovered_with_matching_temporal(self):
        """GIVEN a donation edge whose valid_from matches the externally
        verified ReceivedDate
        WHEN the control is classified
        THEN status is 'recovered' AND temporal_status is 'matched'."""
        donor = Entity.objects.create(
            entity_type="company",
            name="Auvian Limited",
            registry_scheme="GB-COH",
            registry_id="04853169",
            company_number="04853169",
        )
        recipient = Entity.objects.create(
            entity_type="political_party",
            name="Liberal Democrats",
            registry_scheme="EC-REGULATED-ENTITY",
            registry_id="90",
        )
        Edge.objects.create(
            edge_type="donation",
            source_entity=donor,
            target_entity=recipient,
            valid_from=date(2019, 2, 8),
        )
        control = {
            "id": 1,
            "ec_ref": "C0404021",
            "donor_name": "Auvian Limited",
            "donor_company_number": "4853169",
            "recipient_name": "Liberal Democrats",
            "recipient_type": "Political Party",
            "recipient_id": "90",
            "accepted_date": "10/03/2019",
            "received_date": "08/02/2019",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_RECOVERED
        assert row["temporal_status"] == TEMPORAL_MATCHED

    def test_temporal_check_uses_received_date_not_accepted_date(self):
        """GIVEN a donation edge whose valid_from matches AcceptedDate but NOT
        ReceivedDate
        WHEN classified
        THEN temporal_status is 'date_mismatch_or_missing' -- the real
        ec_donations.py ingest sets valid_from from ReceivedDate (falling
        back to AcceptedDate only when ReceivedDate is blank), so comparing
        against the human-facing 'accepted date' alone would misreport a
        correctly-ingested edge as mismatched, or vice versa."""
        donor = Entity.objects.create(
            entity_type="company",
            name="Auvian Limited",
            registry_scheme="GB-COH",
            registry_id="04853169",
            company_number="04853169",
        )
        recipient = Entity.objects.create(
            entity_type="political_party",
            name="Liberal Democrats",
            registry_scheme="EC-REGULATED-ENTITY",
            registry_id="90",
        )
        # Edge dated as the ACCEPTED date -- wrong field for a correctly
        # ingested edge, which would carry the RECEIVED date instead.
        Edge.objects.create(
            edge_type="donation",
            source_entity=donor,
            target_entity=recipient,
            valid_from=date(2019, 3, 10),
        )
        control = {
            "id": 1,
            "donor_company_number": "4853169",
            "recipient_name": "Liberal Democrats",
            "recipient_type": "Political Party",
            "recipient_id": "90",
            "accepted_date": "10/03/2019",
            "received_date": "08/02/2019",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_RECOVERED
        assert row["temporal_status"] == TEMPORAL_MISMATCH

    def test_temporal_falls_back_to_accepted_date_when_received_date_blank(self):
        """GIVEN a control with no ReceivedDate
        WHEN classified against an edge dated with AcceptedDate
        THEN temporal_status is 'matched' -- mirrors ec_donations.py's own
        fallback rule exactly."""
        donor = Entity.objects.create(
            entity_type="company",
            name="Auvian Limited",
            registry_scheme="GB-COH",
            registry_id="04853169",
            company_number="04853169",
        )
        recipient = Entity.objects.create(
            entity_type="political_party",
            name="Liberal Democrats",
            registry_scheme="EC-REGULATED-ENTITY",
            registry_id="90",
        )
        Edge.objects.create(
            edge_type="donation",
            source_entity=donor,
            target_entity=recipient,
            valid_from=date(2019, 3, 10),
        )
        control = {
            "id": 1,
            "donor_company_number": "4853169",
            "recipient_name": "Liberal Democrats",
            "recipient_type": "Political Party",
            "recipient_id": "90",
            "accepted_date": "10/03/2019",
            "received_date": "",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_RECOVERED
        assert row["temporal_status"] == TEMPORAL_MATCHED


@pytest.mark.django_db
class TestClassifyControlNotFound:
    def test_absent_donor_is_not_found(self):
        """GIVEN a recipient that exists but no GB-COH entity for the donor
        company at all
        WHEN classified
        THEN status is 'not-found' and temporal_status is 'not_applicable'
        -- absence is a data-coverage gap, never a retrieval-logic failure."""
        Entity.objects.create(
            entity_type="political_party",
            name="Liberal Democrats",
            registry_scheme="EC-REGULATED-ENTITY",
            registry_id="90",
        )
        control = {
            "id": 1,
            "donor_company_number": "99999999",
            "recipient_name": "Liberal Democrats",
            "recipient_type": "Political Party",
            "recipient_id": "90",
            "accepted_date": "10/03/2019",
            "received_date": "08/02/2019",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_NOT_FOUND
        assert row["temporal_status"] == TEMPORAL_NOT_APPLICABLE

    def test_absent_recipient_is_not_found(self):
        """GIVEN a donor that exists but no matching EC-REGULATED-ENTITY
        recipient
        WHEN classified
        THEN status is 'not-found'."""
        Entity.objects.create(
            entity_type="company",
            name="Auvian Limited",
            registry_scheme="GB-COH",
            registry_id="04853169",
            company_number="04853169",
        )
        control = {
            "id": 1,
            "donor_company_number": "4853169",
            "recipient_name": "Nonexistent Party",
            "recipient_type": "Political Party",
            "recipient_id": "999999",
            "accepted_date": "10/03/2019",
            "received_date": "08/02/2019",
        }

        row = classify_control(control)

        assert row["status"] == STATUS_NOT_FOUND

    def test_both_resolved_but_no_edge_is_not_found(self):
        """GIVEN a donor and recipient that both exist but share no donation
        edge
        WHEN classified
        THEN status is 'not-found'."""
        Entity.objects.create(
            entity_type="company",
            name="Auvian Limited",
            registry_scheme="GB-COH",
            registry_id="04853169",
            company_number="04853169",
        )
        Entity.objects.create(
            entity_type="political_party",
            name="Liberal Democrats",
            registry_scheme="EC-REGULATED-ENTITY",
            registry_id="90",
        )
        control = {
            "id": 1,
            "donor_company_number": "4853169",
            "recipient_name": "Liberal Democrats",
            "recipient_type": "Political Party",
            "recipient_id": "90",
            "accepted_date": "10/03/2019",
            "received_date": "08/02/2019",
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
        donor = Entity.objects.create(
            entity_type="company",
            name="Auvian Limited",
            registry_scheme="GB-COH",
            registry_id="04853169",
            company_number="04853169",
        )
        recipient = Entity.objects.create(
            entity_type="political_party",
            name="Liberal Democrats",
            registry_scheme="EC-REGULATED-ENTITY",
            registry_id="90",
        )
        Edge.objects.create(edge_type="donation", source_entity=donor, target_entity=recipient)
        controls = [
            {
                "id": 1,
                "donor_company_number": "4853169",
                "recipient_name": "Liberal Democrats",
                "recipient_type": "Political Party",
                "recipient_id": "90",
                "accepted_date": "10/03/2019",
                "received_date": "08/02/2019",
            },
            {
                "id": 2,
                "donor_company_number": "99999999",
                "recipient_name": "Nonexistent Party",
                "recipient_type": "Political Party",
                "recipient_id": "999999",
                "accepted_date": "10/03/2019",
                "received_date": "08/02/2019",
            },
        ]
        path = _write_controls(tmp_path, controls)

        first = run_controls(path)
        second = run_controls(path)

        assert [r["status"] for r in first["rows"]] == [r["status"] for r in second["rows"]]
        assert first["retrieval_recovered"] == 1
        assert first["not_found"] == 1
        assert (first["retrieval_recovered"], first["not_found"]) == (
            second["retrieval_recovered"],
            second["not_found"],
        )


@pytest.mark.django_db
class TestRunControlsReportsRetrievalAndTemporalSeparately:
    def test_output_carries_both_figures_with_independent_denominators(self, tmp_path):
        """GIVEN two recovered rows, one with a matching date and one with a
        mismatched date
        WHEN run_controls executes
        THEN retrieval_recovered counts both, temporal_recovered counts only
        the matching one."""
        donor1 = Entity.objects.create(
            entity_type="company",
            name="Donor One",
            registry_scheme="GB-COH",
            registry_id="00000001",
            company_number="00000001",
        )
        recipient1 = Entity.objects.create(
            entity_type="political_party",
            name="Party One",
            registry_scheme="EC-REGULATED-ENTITY",
            registry_id="1",
        )
        Edge.objects.create(
            edge_type="donation",
            source_entity=donor1,
            target_entity=recipient1,
            valid_from=date(2020, 1, 1),
        )

        donor2 = Entity.objects.create(
            entity_type="company",
            name="Donor Two",
            registry_scheme="GB-COH",
            registry_id="00000002",
            company_number="00000002",
        )
        recipient2 = Entity.objects.create(
            entity_type="political_party",
            name="Party Two",
            registry_scheme="EC-REGULATED-ENTITY",
            registry_id="2",
        )
        Edge.objects.create(
            edge_type="donation",
            source_entity=donor2,
            target_entity=recipient2,
            valid_from=date(1999, 1, 1),
        )

        controls = [
            {
                "id": 1,
                "donor_company_number": "00000001",
                "recipient_name": "Party One",
                "recipient_type": "Political Party",
                "recipient_id": "1",
                "accepted_date": "05/01/2020",
                "received_date": "01/01/2020",
            },
            {
                "id": 2,
                "donor_company_number": "00000002",
                "recipient_name": "Party Two",
                "recipient_type": "Political Party",
                "recipient_id": "2",
                "accepted_date": "10/06/2026",
                "received_date": "01/06/2026",
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
