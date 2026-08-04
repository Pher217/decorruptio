"""Tests for spec A2.4.3 per-material-stratum gate measurement.

Covers the delegation packet's required scenarios:
- an unmeasurable stratum is `unavailable`, never `passing`
- a wired stratum with a failing score reports `passing=False`, never
  `unavailable` and never a silent error
- a stratum whose runner cannot execute (missing fixture, malformed fixture,
  runner error) reports `unavailable`
- retrieval and temporal are reported as two distinct figures, never
  conflated
- Lords temporal never reports a pass, regardless of retrieval numbers
- the electoral_commission scoring gap is detected, not silently missed
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from uncorrupt.gates.stratum import (
    StratumMeasurement,
    donation_edges_are_ungated_in_scorer,
    measure_ch_officer_stratum,
    measure_commons_stratum,
    measure_electoral_commission_stratum,
    measure_lords_stratum,
)
from uncorrupt.graph.models import Edge, Entity


class TestStratumMeasurementFailsClosed:
    def test_unavailable_stratum_never_passes_regardless_of_counts(self):
        """GIVEN a stratum explicitly marked unavailable, even with retrieval and
        temporal counts that would otherwise clear the 90% bar
        WHEN passed is evaluated
        THEN it is False -- available=False must dominate everything else
        (ADR-008: "a missing input is an error, never a default pass")."""
        m = StratumMeasurement(
            name="x",
            available=False,
            retrieval_recovered=10,
            retrieval_total=10,
            temporal_recovered=10,
            temporal_total=10,
        )
        assert m.passed is False

    def test_default_construction_is_unavailable(self):
        """GIVEN a StratumMeasurement built with only a name
        WHEN available is checked
        THEN it defaults to False -- a stratum this package never got around to
        measuring must never silently default to passing."""
        m = StratumMeasurement(name="x")
        assert m.available is False
        assert m.passed is False


class TestUnmeasurableStrataAreUnavailable:
    def test_ch_officer_stratum_without_a_fixture_is_unavailable(self, tmp_path):
        """GIVEN no external Companies House temporal control fixture at the given
        path
        WHEN the CH officer stratum is measured
        THEN available is False, not True, and passed is False."""
        missing_path = tmp_path / "does_not_exist.json"

        result = measure_ch_officer_stratum(controls_path=missing_path)

        assert result.available is False
        assert result.passed is False
        assert "no external" in result.note.lower()

    def test_ch_officer_stratum_with_none_path_is_unavailable(self):
        """GIVEN controls_path=None (no fixture configured at all)
        WHEN the CH officer stratum is measured
        THEN it is unavailable."""
        result = measure_ch_officer_stratum(controls_path=None)

        assert result.available is False

    def test_commons_stratum_without_a_fixture_is_unavailable(self, tmp_path):
        """GIVEN no external Commons control fixture at the given path
        WHEN the Commons stratum is measured
        THEN available is False."""
        result = measure_commons_stratum(controls_path=tmp_path / "missing.json")

        assert result.available is False
        assert result.passed is False

    def test_electoral_commission_stratum_without_a_fixture_is_unavailable(self, tmp_path):
        """GIVEN no external Electoral Commission control fixture
        WHEN the electoral_commission stratum is measured
        THEN available is False."""
        result = measure_electoral_commission_stratum(controls_path=tmp_path / "missing.json")

        assert result.available is False

    def test_a_fixture_that_makes_the_runner_error_still_reports_unavailable(self, tmp_path):
        """GIVEN a control fixture file that DOES exist at the configured path, but
        whose content is malformed (a control row missing required fields), so
        scripts/run_ch_controls.py's run_controls() raises while executing it
        WHEN the CH officer stratum is measured
        THEN it is still reported unavailable, never passing and never a silent
        zero -- a fixture existing is not sufficient by itself; the wired runner
        must actually be able to execute against it (CH now HAS a wired runner,
        scripts/run_ch_controls.py -- unlike the old fixture-alone check this
        replaces, this asserts the runner-cannot-execute case specifically)."""
        fixture_path = tmp_path / "ch_temporal_controls.json"
        fixture_path.write_text(
            json.dumps({"controls": [{"id": 1}]}), encoding="utf-8"
        )  # missing officer_id/company_number/appointed_on -- run_controls() raises KeyError

        result = measure_ch_officer_stratum(controls_path=fixture_path)

        assert result.available is False
        assert result.passed is False
        assert "run_ch_controls.py" in result.note
        assert "KeyError" in result.note


@pytest.mark.django_db
class TestWiredStratumRunnersReportRealScores:
    """Now that CH, Commons, and EC all have a wired scripts/run_*_controls.py
    runner (mirroring Lords), a stratum with a real fixture and a reachable
    graph must report the ACTUAL measured score -- available, with whatever
    passed/failed result that score implies -- never unavailable and never a
    silently-defaulted zero."""

    def test_ch_officer_stratum_with_a_partial_score_reports_available_and_not_passing(
        self, tmp_path
    ):
        """GIVEN a 2-row CH control fixture where only one row's officer/company/
        appointment is present in the graph
        WHEN the CH officer stratum is measured
        THEN available is True (the runner executed for real), retrieval is
        1/2 (below the 90% bar) so passed is False -- a wired stratum with a
        failing score is reported honestly, not as unavailable and not as an
        error."""
        officer = Entity.objects.create(
            entity_type="person",
            name="BOWDEN, Matthew Shaun",
            registry_scheme="GB-COH-OFFICER",
            registry_id="officer-1",
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

        fixture = {
            "controls": [
                {
                    "id": 1,
                    "officer_id": "officer-1",
                    "officer_name": "BOWDEN, Matthew Shaun",
                    "company_number": "02723534",
                    "company_name": "ASTRAZENECA PLC",
                    "appointed_on": "2025-05-01",
                },
                {
                    "id": 2,
                    "officer_id": "officer-not-in-graph",
                    "officer_name": "Nobody",
                    "company_number": "99999999",
                    "company_name": "Nowhere Ltd",
                    "appointed_on": "2020-01-01",
                },
            ]
        }
        fixture_path = tmp_path / "ch_temporal_controls.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        result = measure_ch_officer_stratum(controls_path=fixture_path)

        assert result.available is True
        assert result.retrieval_recovered == 1
        assert result.retrieval_total == 2
        assert result.passed is False

    def test_retrieval_and_temporal_are_reported_as_distinct_figures(self, tmp_path):
        """GIVEN the same 1-of-2-recovered CH fixture as above, where the one
        recovered control's date also matches
        WHEN the CH officer stratum is measured
        THEN temporal_total is scoped to the RETRIEVED subset (1), not the raw
        control-battery size (2) -- retrieval_total and temporal_total are
        genuinely different numbers, proving the two figures are never
        conflated."""
        officer = Entity.objects.create(
            entity_type="person",
            name="BOWDEN, Matthew Shaun",
            registry_scheme="GB-COH-OFFICER",
            registry_id="officer-1",
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
        fixture = {
            "controls": [
                {
                    "id": 1,
                    "officer_id": "officer-1",
                    "officer_name": "BOWDEN, Matthew Shaun",
                    "company_number": "02723534",
                    "company_name": "ASTRAZENECA PLC",
                    "appointed_on": "2025-05-01",
                },
                {
                    "id": 2,
                    "officer_id": "officer-not-in-graph",
                    "officer_name": "Nobody",
                    "company_number": "99999999",
                    "company_name": "Nowhere Ltd",
                    "appointed_on": "2020-01-01",
                },
            ]
        }
        fixture_path = tmp_path / "ch_temporal_controls.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        result = measure_ch_officer_stratum(controls_path=fixture_path)

        assert result.retrieval_total == 2
        assert result.temporal_total == 1
        assert result.retrieval_total != result.temporal_total
        assert result.temporal_recovered == 1
        assert result.temporal_passed is True
        assert result.retrieval_passed is False
        assert result.passed is False  # available AND retrieval_passed AND temporal_passed

    def test_commons_stratum_with_wired_runner_reports_a_real_recovered_score(self, tmp_path):
        """GIVEN a 1-row Commons control fixture whose member/organisation/edge
        ARE present in the graph
        WHEN the Commons stratum is measured
        THEN available is True and retrieval is 1/1 -- scripts/run_commons_controls
        .py is the correct runner wired for this stratum, not a copy-paste of
        another stratum's runner."""
        member = Entity.objects.create(
            entity_type="person",
            name="Ms Stella Creasy",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="4088",
        )
        org = Entity.objects.create(
            entity_type="company",
            name="Guardian News And Media",
            registry_scheme="GB-COH",
            registry_id="00000009",
            company_number="00000009",
        )
        Edge.objects.create(
            edge_type="declared_interest",
            source_entity=member,
            target_entity=org,
            valid_from=date(2026, 1, 29),
        )

        fixture = {
            "controls": [
                {
                    "id": 1,
                    "interest_id": 5336,
                    "member_id": 4088,
                    "member_name": "Ms Stella Creasy",
                    "organisation_name": "Guardian News And Media",
                    "company_number": None,
                    "registration_date": "2026-01-29",
                }
            ]
        }
        fixture_path = tmp_path / "commons_retrieval_controls.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        result = measure_commons_stratum(controls_path=fixture_path)

        assert result.available is True
        assert result.retrieval_recovered == 1
        assert result.retrieval_total == 1

    def test_electoral_commission_stratum_with_wired_runner_reports_a_real_recovered_score(
        self, tmp_path
    ):
        """GIVEN a 1-row EC control fixture whose donor/recipient/edge ARE
        present in the graph
        WHEN the electoral_commission stratum is measured
        THEN available is True and retrieval is 1/1 -- scripts/run_ec_controls.py
        is the correct runner wired for this stratum."""
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

        fixture = {
            "controls": [
                {
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
            ]
        }
        fixture_path = tmp_path / "ec_retrieval_controls.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        result = measure_electoral_commission_stratum(controls_path=fixture_path)

        assert result.available is True
        assert result.retrieval_recovered == 1
        assert result.retrieval_total == 1


@pytest.mark.django_db
class TestLordsTemporalNeverReportsAPass:
    def test_temporal_fields_are_none_regardless_of_retrieval_result(self, tmp_path):
        """GIVEN a Lords control fixture where every control recovers (perfect
        retrieval)
        WHEN the Lords stratum is measured
        THEN temporal_recovered and temporal_total are both None, and
        temporal_passed / passed are False -- retrieval performance can never
        flip the temporal endpoint, per spec A2.5.1/v2.9."""
        peer = Entity.objects.create(
            entity_type="person",
            name="Lord Testington",
            registry_scheme="UK-PARLIAMENT-MEMBER",
            registry_id="1",
        )
        company = Entity.objects.create(
            entity_type="company",
            name="Acme Widgets Ltd",
            registry_scheme="GB-COH",
            registry_id="01234567",
            company_number="01234567",
        )
        Edge.objects.create(
            edge_type="declared_interest", source_entity=peer, target_entity=company
        )

        fixture = {
            "controls": [
                {
                    "id": 1,
                    "page": 1,
                    "member_id": "1",
                    "peer_name": "Lord Testington",
                    "declared_company": "Acme Widgets Ltd",
                }
            ]
        }
        fixture_path = tmp_path / "lords_controls.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        result = measure_lords_stratum(controls_path=fixture_path)

        assert result.retrieval_recovered == 1
        assert result.retrieval_total == 1
        assert result.retrieval_passed is True
        assert result.temporal_recovered is None
        assert result.temporal_total is None
        assert result.temporal_passed is False
        assert result.passed is False

    def test_zero_retrieval_still_leaves_temporal_unset_not_zero(self, tmp_path):
        """GIVEN a Lords control fixture where nothing resolves (zero retrieval)
        WHEN the Lords stratum is measured
        THEN temporal fields remain None -- never coerced to 0/0 or any other
        value that could later be mistaken for a measured temporal result."""
        fixture = {
            "controls": [
                {
                    "id": 1,
                    "page": 1,
                    "member_id": "999",
                    "peer_name": "Lord Nobody",
                    "declared_company": "Nonexistent Ltd",
                }
            ]
        }
        fixture_path = tmp_path / "lords_controls.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        result = measure_lords_stratum(controls_path=fixture_path)

        assert result.retrieval_recovered == 0
        assert result.temporal_recovered is None
        assert result.temporal_total is None
        assert result.passed is False


class TestElectoralCommissionScoringGap:
    def test_donation_edges_are_currently_ungated_in_the_scorer(self):
        """GIVEN the current, unedited run_gold_benchmark.MATERIAL_STRATA
        WHEN donation_edges_are_ungated_in_scorer is checked
        THEN it is True -- electoral_commission is not one of the three material
        strata, so a donation edge on a mixed path can qualify through another
        stratum's passing gate with its own evidence unvalidated."""
        assert donation_edges_are_ungated_in_scorer() is True
