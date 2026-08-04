"""Tests for the ADR-008 no-score certificate.

Covers the delegation packet's required scenario: a failing gate produces a
no-score certificate naming the blocker.
"""

from __future__ import annotations

import json

from uncorrupt.gates.binding import GateFreezeState
from uncorrupt.gates.certificate import build_no_score_certificate, write_no_score_certificate
from uncorrupt.gates.coverage import CoverageMeasurement
from uncorrupt.gates.stratum import StratumMeasurement


def _freeze_state() -> GateFreezeState:
    return GateFreezeState(
        code_commit="abc123",
        graph_hash="graphhash",
        attestation_inclusive_hash="attesthash",
        manifest_hash="manifesthash",
        measured_at="2026-08-03T00:00:00+00:00",
    )


class TestBuildNoScoreCertificate:
    def test_no_certificate_when_everything_passes(self):
        """GIVEN a passing coverage measurement and a passing, available stratum
        WHEN a no-score certificate is built
        THEN the result is None -- nothing blocks scoring."""
        coverage = {
            "x": CoverageMeasurement(
                name="x", ingested=10, explicitly_failed=0, not_attempted=0, total=10
            )
        }
        strata = {
            "y": StratumMeasurement(
                name="y",
                available=True,
                retrieval_recovered=9,
                retrieval_total=10,
                temporal_recovered=9,
                temporal_total=10,
            )
        }

        certificate = build_no_score_certificate(
            _freeze_state(), coverage_measurements=coverage, stratum_measurements=strata
        )

        assert certificate is None

    def test_failing_coverage_gate_names_the_blocker(self):
        """GIVEN a coverage measurement with a partial ingest (fails the gate)
        WHEN a no-score certificate is built
        THEN it names that exact gate as a blocker, with a reason and detail."""
        coverage = {
            "companies_house_officer_roster": CoverageMeasurement(
                name="companies_house_officer_roster",
                ingested=5,
                explicitly_failed=0,
                not_attempted=95,
                total=100,
            )
        }

        certificate = build_no_score_certificate(_freeze_state(), coverage_measurements=coverage)

        assert certificate is not None
        assert certificate["no_score"] is True
        gates_named = [b["gate"] for b in certificate["blockers"]]
        assert "coverage:companies_house_officer_roster" in gates_named
        blocker = next(
            b
            for b in certificate["blockers"]
            if b["gate"] == "coverage:companies_house_officer_roster"
        )
        assert blocker["detail"]["not_attempted"] == 95
        assert blocker["detail"]["total"] == 100

    def test_unavailable_stratum_names_the_blocker(self):
        """GIVEN a stratum measurement with available=False
        WHEN a no-score certificate is built
        THEN it names that stratum as a blocker with a 'no external control
        battery available' reason."""
        strata = {"ch_officer_appointment": StratumMeasurement(name="ch_officer_appointment")}

        certificate = build_no_score_certificate(_freeze_state(), stratum_measurements=strata)

        assert certificate is not None
        gates_named = [b["gate"] for b in certificate["blockers"]]
        assert "stratum:ch_officer_appointment" in gates_named
        blocker = next(
            b for b in certificate["blockers"] if b["gate"] == "stratum:ch_officer_appointment"
        )
        assert "no external control battery" in blocker["reason"]

    def test_failing_stratum_gate_names_the_blocker_with_its_exact_score(self):
        """GIVEN a stratum measurement that IS available (a real 12-row control
        battery ran) but scores below the >=9/10 gate -- the real measured
        Companies House shape (7/12 retrieval, 7/12 temporal, spec amendment
        v2.10)
        WHEN a no-score certificate is built
        THEN it names that exact stratum as a blocker and the blocker detail
        carries the real recovered/total counts, not just a boolean."""
        strata = {
            "ch_officer_appointment": StratumMeasurement(
                name="ch_officer_appointment",
                available=True,
                retrieval_recovered=7,
                retrieval_total=12,
                temporal_recovered=7,
                temporal_total=12,
            )
        }

        certificate = build_no_score_certificate(_freeze_state(), stratum_measurements=strata)

        assert certificate is not None
        blocker = next(
            b for b in certificate["blockers"] if b["gate"] == "stratum:ch_officer_appointment"
        )
        assert blocker["detail"]["retrieval_recovered"] == 7
        assert blocker["detail"]["retrieval_total"] == 12
        assert blocker["detail"]["temporal_recovered"] == 7
        assert blocker["detail"]["temporal_total"] == 12

    def test_certificate_cannot_claim_a_pass_when_a_gate_failed(self):
        """GIVEN a stratum battery with one measured, failing gate (CH 7/12,
        below the 90% bar) alongside one passing gate (Electoral Commission
        11/12)
        WHEN a no-score certificate is built
        THEN the certificate is emitted with no_score=True -- there is no code
        path in which a failing measured gate produces a certificate claiming
        overall success; a passing sibling stratum never masks the failure."""
        strata = {
            "ch_officer_appointment": StratumMeasurement(
                name="ch_officer_appointment",
                available=True,
                retrieval_recovered=7,
                retrieval_total=12,
                temporal_recovered=7,
                temporal_total=12,
            ),
            "electoral_commission": StratumMeasurement(
                name="electoral_commission",
                available=True,
                retrieval_recovered=11,
                retrieval_total=12,
                temporal_recovered=11,
                temporal_total=12,
            ),
        }

        certificate = build_no_score_certificate(_freeze_state(), stratum_measurements=strata)

        assert certificate is not None
        assert certificate["no_score"] is True
        gates_named = {b["gate"] for b in certificate["blockers"]}
        assert gates_named == {"stratum:ch_officer_appointment"}
        assert "stratum:electoral_commission" not in gates_named

    def test_certificate_binds_to_the_freeze_state(self):
        """GIVEN a freeze state
        WHEN a no-score certificate is built for a failing gate
        THEN the certificate's freeze_state matches exactly."""
        coverage = {
            "x": CoverageMeasurement(
                name="x", ingested=0, explicitly_failed=0, not_attempted=1, total=1
            )
        }
        state = _freeze_state()

        certificate = build_no_score_certificate(state, coverage_measurements=coverage)

        assert certificate["freeze_state"] == state.to_binding_dict()

    def test_multiple_blockers_are_all_named_independently(self):
        """GIVEN two failing gates -- one coverage, one stratum
        WHEN a no-score certificate is built
        THEN both are named as independent blockers."""
        coverage = {
            "commons_register": CoverageMeasurement(
                name="commons_register",
                ingested=25,
                explicitly_failed=0,
                not_attempted=4032,
                total=4057,
            )
        }
        strata = {"lords_declared_interest": StratumMeasurement(name="lords_declared_interest")}

        certificate = build_no_score_certificate(
            _freeze_state(), coverage_measurements=coverage, stratum_measurements=strata
        )

        gates_named = {b["gate"] for b in certificate["blockers"]}
        assert gates_named == {"coverage:commons_register", "stratum:lords_declared_interest"}

    def test_no_measurements_at_all_produces_no_certificate(self):
        """GIVEN neither coverage nor stratum measurements were passed
        WHEN a no-score certificate is built
        THEN the result is None -- a caller that measured nothing has nothing to
        report as blocking, distinct from "everything passed"."""
        certificate = build_no_score_certificate(_freeze_state())

        assert certificate is None


class TestWriteNoScoreCertificate:
    def test_writes_valid_json_to_the_given_path(self, tmp_path):
        """GIVEN a built certificate dict
        WHEN it is written to a path under a not-yet-existing directory
        THEN the directory is created and the file contains the same JSON."""
        coverage = {
            "x": CoverageMeasurement(
                name="x", ingested=0, explicitly_failed=0, not_attempted=1, total=1
            )
        }
        certificate = build_no_score_certificate(_freeze_state(), coverage_measurements=coverage)
        out_path = tmp_path / "nested" / "certificate.json"

        written_path = write_no_score_certificate(out_path, certificate)

        assert written_path == out_path
        assert json.loads(out_path.read_text(encoding="utf-8")) == certificate
