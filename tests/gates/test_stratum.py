"""Tests for spec A2.4.3 per-material-stratum gate measurement.

Covers the delegation packet's required scenarios:
- an unmeasurable stratum is `unavailable`, never `passing`
- Lords temporal never reports a pass, regardless of retrieval numbers
- the electoral_commission scoring gap is detected, not silently missed
"""

from __future__ import annotations

import json

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

    def test_a_fixture_existing_without_a_wired_runner_still_reports_unavailable(self, tmp_path):
        """GIVEN a control fixture file that DOES exist at the configured path, but
        no control-battery runner is implemented for that stratum (true for CH
        today -- only Lords has scripts/run_lords_controls.py)
        WHEN the CH officer stratum is measured
        THEN it is still reported unavailable, not silently treated as passing
        just because a file happens to exist there."""
        fixture_path = tmp_path / "ch_temporal_controls.json"
        fixture_path.write_text(json.dumps({"controls": []}), encoding="utf-8")

        result = measure_ch_officer_stratum(controls_path=fixture_path)

        assert result.available is False


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
