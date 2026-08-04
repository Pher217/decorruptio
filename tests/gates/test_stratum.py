"""Tests for spec A2.4.3 per-material-stratum gate measurement.

Covers the delegation packet's required scenarios:
- an unmeasurable stratum is `unavailable`, never `passing`
- a wired stratum with a failing score reports `passing=False`, never
  `unavailable` and never a silent error
- a stratum whose runner cannot execute (missing fixture, malformed fixture,
  runner error) reports `unavailable`
- retrieval and temporal are independently evaluated, never conflated
- Lords temporal never reports a pass, regardless of retrieval numbers
- the electoral_commission scoring gap is detected, not silently missed

Plus the independent-review findings this file was rewritten to close:
- a control battery smaller than the pre-registered size (12) can never
  produce `available=True`, no matter how well its rows score -- an
  arbitrary 1-row (or empty) `--ch-controls`/`--commons-controls`/
  `--ec-controls` fixture must never unlock a material stratum
- `compute_control_fixtures_hash` changes when a fixture's content changes,
  closing the "the fixture is unbound" gap (code_commit/graph_hash/
  attestation_inclusive_hash/manifest_hash are all fixture-blind)
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from uncorrupt.gates.stratum import (
    MIN_CONTROL_BATTERY_SIZE,
    StratumMeasurement,
    compute_control_fixtures_hash,
    donation_edges_are_ungated_in_scorer,
    measure_ch_officer_stratum,
    measure_commons_stratum,
    measure_electoral_commission_stratum,
    measure_lords_stratum,
)
from uncorrupt.graph.models import Edge, Entity

assert MIN_CONTROL_BATTERY_SIZE == 12, (
    "these tests hardcode battery sizes around the pre-registered 12-row size -- if this "
    "constant ever changes, the fixtures below must be resized to match, not silently pass "
    "against a stale assumption."
)


def _ch_not_found_rows(n: int, offset: int = 900) -> list[dict]:
    """`n` CH control rows guaranteed not to resolve against any graph entity
    -- cheap filler to reach MIN_CONTROL_BATTERY_SIZE without creating a real
    Entity/Edge for every row."""
    return [
        {
            "id": offset + i,
            "officer_id": f"nonexistent-officer-{offset + i}",
            "officer_name": f"Nobody {offset + i}",
            "company_number": f"{80000000 + offset + i:08d}",
            "company_name": f"Nowhere {offset + i} Ltd",
            "appointed_on": "2020-01-01",
        }
        for i in range(n)
    ]


def _commons_not_found_rows(n: int, offset: int = 900) -> list[dict]:
    """`n` Commons control rows guaranteed not to resolve."""
    return [
        {
            "id": offset + i,
            "interest_id": offset + i,
            "member_id": 900000 + offset + i,
            "member_name": f"Nobody MP {offset + i}",
            "organisation_name": f"Nonexistent Org {offset + i} Ltd",
            "company_number": None,
            "registration_date": "2020-01-01",
        }
        for i in range(n)
    ]


def _ec_not_found_rows(n: int, offset: int = 900) -> list[dict]:
    """`n` EC control rows guaranteed not to resolve."""
    return [
        {
            "id": offset + i,
            "ec_ref": f"NONE{offset + i}",
            "donor_name": f"Nonexistent Donor {offset + i} Ltd",
            "donor_company_number": f"{70000000 + offset + i:08d}",
            "recipient_name": f"Nonexistent Party {offset + i}",
            "recipient_type": "Political Party",
            "recipient_id": f"nonexistent-{offset + i}",
            "accepted_date": "01/01/2020",
            "received_date": "",
        }
        for i in range(n)
    ]


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
    def test_a_1_row_fixture_cannot_produce_available_true_even_if_the_row_recovers(self, tmp_path):
        """GIVEN a control fixture with exactly ONE row, and that one row's
        officer/company/appointment ARE present in the graph (it would retrieve
        AND temporal-match if it were allowed to run)
        WHEN the CH officer stratum is measured
        THEN available is still False -- an independent review demonstrated
        that without this floor, an arbitrary 1-row fixture reports
        available=True, passed=True even though the real 12-row battery for
        this same stratum scores 6/12 and FAILS. A battery below
        MIN_CONTROL_BATTERY_SIZE is an untrusted input, not a passing (or
        failing) score."""
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
                }
            ]
        }
        fixture_path = tmp_path / "ch_temporal_controls.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        result = measure_ch_officer_stratum(controls_path=fixture_path)

        assert result.available is False
        assert result.passed is False
        assert "12" in result.note
        assert "1-row" in result.note or "1 " in result.note or "battery" in result.note.lower()

    def test_empty_battery_reports_unavailable_not_a_score_shaped_blocker(self, tmp_path):
        """GIVEN a control fixture with zero rows ({"controls": []}) -- the
        runner CAN execute against it (no exception), it just measures nothing
        WHEN the CH officer stratum is measured
        THEN available is False, never True -- an executed-but-empty battery
        must never be mistaken for a battery that legitimately passed or
        legitimately failed; it is below MIN_CONTROL_BATTERY_SIZE like any
        other undersized fixture."""
        fixture_path = tmp_path / "ch_temporal_controls.json"
        fixture_path.write_text(json.dumps({"controls": []}), encoding="utf-8")

        result = measure_ch_officer_stratum(controls_path=fixture_path)

        assert result.available is False
        assert result.passed is False


@pytest.mark.django_db
class TestWiredStratumRunnersReportRealScores:
    """Now that CH, Commons, and EC all have a wired scripts/run_*_controls.py
    runner (mirroring Lords), a stratum with a real, pre-registered-sized (12
    row) fixture and a reachable graph must report the ACTUAL measured score
    -- available, with whatever passed/failed result that score implies --
    never unavailable and never a silently-defaulted zero. Every fixture here
    is exactly MIN_CONTROL_BATTERY_SIZE (12) rows, matching the real
    tests/fixtures/*_controls.json battery size -- these are NOT the
    small/arbitrary fixtures the battery-size floor rejects."""

    def test_ch_officer_stratum_with_a_failing_score_reports_available_and_not_passing(
        self, tmp_path
    ):
        """GIVEN a 12-row CH control fixture where only 5 rows' officer/company/
        appointment are present in the graph
        WHEN the CH officer stratum is measured
        THEN available is True (the runner executed for real, against a
        battery at the pre-registered size), retrieval is 5/12 (below the 90%
        bar) so passed is False -- a wired stratum with a failing score is
        reported honestly, not as unavailable and not as an error."""
        recovered_rows = []
        for i in range(5):
            officer = Entity.objects.create(
                entity_type="person",
                name=f"Officer {i}",
                registry_scheme="GB-COH-OFFICER",
                registry_id=f"officer-{i}",
            )
            company_number = f"{10000000 + i:08d}"
            company = Entity.objects.create(
                entity_type="company",
                name=f"Company {i}",
                registry_scheme="GB-COH",
                registry_id=company_number,
                company_number=company_number,
            )
            Edge.objects.create(
                edge_type="officer_of",
                source_entity=officer,
                target_entity=company,
                valid_from=date(2025, 5, 1),
            )
            recovered_rows.append(
                {
                    "id": i,
                    "officer_id": f"officer-{i}",
                    "officer_name": f"Officer {i}",
                    "company_number": company_number,
                    "company_name": f"Company {i}",
                    "appointed_on": "2025-05-01",
                }
            )
        fixture = {"controls": recovered_rows + _ch_not_found_rows(7)}
        fixture_path = tmp_path / "ch_temporal_controls.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        result = measure_ch_officer_stratum(controls_path=fixture_path)

        assert result.available is True
        assert result.retrieval_recovered == 5
        assert result.retrieval_total == 12
        assert result.passed is False

    def test_retrieval_and_temporal_are_independently_evaluated(self, tmp_path):
        """GIVEN a 12-row CH control fixture where 11 rows retrieve (>=90%,
        retrieval PASSES) but only 9 of those 11 carry a correctly-dated edge
        (<90% of 12, temporal FAILS)
        WHEN the CH officer stratum is measured
        THEN retrieval_passed is True, temporal_passed is False, and
        retrieval_total == temporal_total (both the SAME raw battery size,
        never rescaled) -- proving retrieval and temporal are independently
        computed outcomes, not that they use different denominators. This is
        the corrected form of the earlier (exploitable) design: an
        independent review showed rescaling temporal_total to the retrieved
        subset let a worse retrieval shrink the temporal denominator and
        flip a genuine FAIL into a PASS."""
        rows = []
        for i in range(11):
            officer = Entity.objects.create(
                entity_type="person",
                name=f"Officer {i}",
                registry_scheme="GB-COH-OFFICER",
                registry_id=f"officer-{i}",
            )
            company_number = f"{20000000 + i:08d}"
            company = Entity.objects.create(
                entity_type="company",
                name=f"Company {i}",
                registry_scheme="GB-COH",
                registry_id=company_number,
                company_number=company_number,
            )
            # First 9 rows: edge dated EXACTLY as the fixture claims (temporal match).
            # Last 2 of the 11: edge dated differently (retrieved, temporal mismatch).
            edge_valid_from = date(2025, 5, 1) if i < 9 else date(2019, 1, 1)
            Edge.objects.create(
                edge_type="officer_of",
                source_entity=officer,
                target_entity=company,
                valid_from=edge_valid_from,
            )
            rows.append(
                {
                    "id": i,
                    "officer_id": f"officer-{i}",
                    "officer_name": f"Officer {i}",
                    "company_number": company_number,
                    "company_name": f"Company {i}",
                    "appointed_on": "2025-05-01",
                }
            )
        fixture = {"controls": rows + _ch_not_found_rows(1)}
        fixture_path = tmp_path / "ch_temporal_controls.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        result = measure_ch_officer_stratum(controls_path=fixture_path)

        assert result.retrieval_recovered == 11
        assert result.retrieval_total == 12
        assert result.retrieval_passed is True
        assert result.temporal_recovered == 9
        assert result.temporal_total == 12
        assert result.temporal_total == result.retrieval_total  # same raw battery size, shared
        assert result.temporal_passed is False
        assert result.passed is False  # available AND retrieval_passed AND temporal_passed

    def test_commons_stratum_with_wired_runner_reports_a_real_recovered_score(self, tmp_path):
        """GIVEN a 12-row Commons control fixture where one row's member/
        organisation/edge ARE present in the graph and 11 are not
        WHEN the Commons stratum is measured
        THEN available is True and retrieval is 1/12 -- scripts/run_commons_controls
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

        recovered_row = {
            "id": 1,
            "interest_id": 5336,
            "member_id": 4088,
            "member_name": "Ms Stella Creasy",
            "organisation_name": "Guardian News And Media",
            "company_number": None,
            "registration_date": "2026-01-29",
        }
        fixture = {"controls": [recovered_row] + _commons_not_found_rows(11)}
        fixture_path = tmp_path / "commons_retrieval_controls.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        result = measure_commons_stratum(controls_path=fixture_path)

        assert result.available is True
        assert result.retrieval_recovered == 1
        assert result.retrieval_total == 12

    def test_electoral_commission_stratum_with_wired_runner_reports_a_real_recovered_score(
        self, tmp_path
    ):
        """GIVEN a 12-row EC control fixture where one row's donor/recipient/
        edge ARE present in the graph and 11 are not
        WHEN the electoral_commission stratum is measured
        THEN available is True and retrieval is 1/12 -- scripts/run_ec_controls.py
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

        recovered_row = {
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
        fixture = {"controls": [recovered_row] + _ec_not_found_rows(11)}
        fixture_path = tmp_path / "ec_retrieval_controls.json"
        fixture_path.write_text(json.dumps(fixture), encoding="utf-8")

        result = measure_electoral_commission_stratum(controls_path=fixture_path)

        assert result.available is True
        assert result.retrieval_recovered == 1
        assert result.retrieval_total == 12


class TestComputeControlFixturesHash:
    """Closes the "the fixture is unbound" gap: code_commit/graph_hash/
    attestation_inclusive_hash/manifest_hash all leave a control fixture free
    to be edited (or substituted) without changing any of them --
    compute_control_fixtures_hash is what GateFreezeState.control_fixtures_hash
    (uncorrupt.gates.binding) binds to instead."""

    def _write(self, path, content: str) -> None:
        path.write_text(content, encoding="utf-8")

    def test_hash_changes_when_a_fixture_is_edited(self, tmp_path):
        """GIVEN two otherwise-identical fixture sets differing in the content of
        just one file
        WHEN compute_control_fixtures_hash is computed for each
        THEN the hashes differ -- an edited-but-uncommitted fixture is
        detectable even though it changes no git commit."""
        ch_path = tmp_path / "ch.json"
        self._write(ch_path, json.dumps({"controls": [{"id": 1}]}))
        commons_path = tmp_path / "commons.json"
        self._write(commons_path, json.dumps({"controls": []}))

        before = compute_control_fixtures_hash(
            lords_controls_path=None,
            ch_controls_path=ch_path,
            commons_controls_path=commons_path,
            ec_controls_path=None,
        )

        self._write(ch_path, json.dumps({"controls": [{"id": 1, "tampered": True}]}))

        after = compute_control_fixtures_hash(
            lords_controls_path=None,
            ch_controls_path=ch_path,
            commons_controls_path=commons_path,
            ec_controls_path=None,
        )

        assert before != after

    def test_hash_is_stable_for_unchanged_fixtures(self, tmp_path):
        """GIVEN the same fixture paths and content, computed twice
        WHEN compute_control_fixtures_hash is called each time
        THEN the two hashes are identical -- a real, reproducible content
        hash, not a timestamp or random value."""
        ch_path = tmp_path / "ch.json"
        self._write(ch_path, json.dumps({"controls": [{"id": 1}]}))

        first = compute_control_fixtures_hash(
            lords_controls_path=None,
            ch_controls_path=ch_path,
            commons_controls_path=None,
            ec_controls_path=None,
        )
        second = compute_control_fixtures_hash(
            lords_controls_path=None,
            ch_controls_path=ch_path,
            commons_controls_path=None,
            ec_controls_path=None,
        )

        assert first == second

    def test_a_missing_fixture_hashes_differently_than_none(self, tmp_path):
        """GIVEN one call where a control path is None (not configured) and
        another where it points at a file that does not exist (configured but
        missing)
        WHEN compute_control_fixtures_hash is computed for each
        THEN the two hashes differ -- "not configured" and "configured but
        missing" must never silently collide to the same value."""
        missing_path = tmp_path / "does_not_exist.json"

        with_none = compute_control_fixtures_hash(
            lords_controls_path=None,
            ch_controls_path=None,
            commons_controls_path=None,
            ec_controls_path=None,
        )
        with_missing_path = compute_control_fixtures_hash(
            lords_controls_path=None,
            ch_controls_path=missing_path,
            commons_controls_path=None,
            ec_controls_path=None,
        )

        assert with_none != with_missing_path


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
