"""Tests for `scripts/emit_no_score_certificate.py`'s own added logic --
the pieces `uncorrupt.gates.certificate.build_no_score_certificate` does not
compute: the Companies House structural-ceiling finding, the threshold
arithmetic table, the manifest-hash fallback, the sealed-cohort statement,
the Electoral Commission materiality caveat, the coverage-gate-family
loader/note (spec A2.4.2), and the SystemExit refusal when every measured
stratum unexpectedly passes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest
from scripts.emit_no_score_certificate import (
    GATE_FRACTION,
    REQUIRED_COVERAGE_FAMILIES,
    _load_coverage_gate_measurements,
    _manifest_hash_or_unavailable,
    ch_structural_ceiling,
    coverage_gate_note,
    electoral_commission_materiality_note,
    sealed_cohort_statement,
    threshold_arithmetic_table,
)
from scripts.run_gold_benchmark import compute_manifest_hash

from uncorrupt.gates.binding import GateFreezeState
from uncorrupt.gates.coverage import CoverageMeasurement
from uncorrupt.gates.stratum import StratumMeasurement
from uncorrupt.staging.models import Company


def _write_ch_fixture(tmp_path: Path, controls: list[dict]) -> Path:
    path = tmp_path / "ch_controls.json"
    path.write_text(json.dumps({"controls": controls}), encoding="utf-8")
    return path


@pytest.mark.django_db
class TestChStructuralCeiling:
    def test_identifies_rows_with_no_company_staging_row_as_structurally_blocked(self, tmp_path):
        """GIVEN a 4-row CH control fixture where 2 rows cite a company_number
        with NO staging.Company row at all (legacy NF-prefixed identifiers,
        mirroring the real British Airways/Marks & Spencer rows) and 2 cite a
        company_number that DOES have a Company row
        WHEN the structural ceiling is computed
        THEN exactly the 2 Company-less rows are reported structurally
        blocked, and the other 2 are not -- even though neither of those 2
        has a graph Entity yet (a coverage gap is not a structural one)."""
        Company.objects.create(company_number="10041931", company_name="ASHTEAD TECHNOLOGY")
        Company.objects.create(company_number="12215835", company_name="GLAXOSMITHKLINE")

        fixture = _write_ch_fixture(
            tmp_path,
            [
                {"id": 1, "company_number": "10041931", "company_name": "ASHTEAD TECHNOLOGY"},
                {"id": 2, "company_number": "NF002699", "company_name": "BRITISH AIRWAYS PLC"},
                {"id": 3, "company_number": "12215835", "company_name": "GLAXOSMITHKLINE"},
                {"id": 4, "company_number": "NF001553", "company_name": "MARKS AND SPENCER"},
            ],
        )

        result = ch_structural_ceiling(fixture)

        blocked_numbers = {r["company_number"] for r in result["structurally_blocked_rows"]}
        assert blocked_numbers == {"NF002699", "NF001553"}
        assert result["structurally_blocked_count"] == 2
        assert result["total_controls"] == 4

    def test_max_achievable_recovered_is_total_minus_structurally_blocked(self, tmp_path):
        """GIVEN a small 3-control fixture -- 1 control with a Company row, 2
        without (NF-prefixed, structurally blocked)
        WHEN the structural ceiling is computed
        THEN max_achievable_recovered is total minus blocked (1) and
        max_achievable_pct is the correctly rounded 1/3 (33.3%) -- proves the
        arithmetic, not just the row-classification, is right. (The real
        12-row shape -- 2 blocked, 10 not, 83.3% -- is covered separately by
        test_ceiling_fails_the_gate_when_below_90_percent below.)"""
        Company.objects.create(company_number="10041931", company_name="X")
        controls = [{"id": 1, "company_number": "10041931", "company_name": "X"}]
        controls += [
            {"id": i, "company_number": f"NF00{i:04d}", "company_name": f"Y{i}"}
            for i in range(2, 4)
        ]
        fixture = _write_ch_fixture(tmp_path, controls)

        result = ch_structural_ceiling(fixture)

        assert result["max_achievable_recovered"] == 1
        assert result["total_controls"] == 3
        assert result["max_achievable_pct"] == 33.3

    def test_normalises_the_company_number_before_checking_for_a_company_row(self, tmp_path):
        """GIVEN a control whose company_number is UNPADDED (as an external
        source might supply it, e.g. "7015428" for Companies House's
        canonical zero-padded "07015428") and a Company row that exists under
        the CANONICAL, zero-padded form
        WHEN the structural ceiling is computed
        THEN that control is NOT reported structurally blocked -- proves the
        normalise_company_number call is load-bearing. Without it, an exact
        string match on "7015428" would miss the real "07015428" Company row
        and falsely report an ingested company as structurally absent (the
        other three tests in this class all use already-8-character numbers,
        so none of them would catch that regression)."""
        Company.objects.create(company_number="07015428", company_name="Padded Co")
        fixture = _write_ch_fixture(
            tmp_path, [{"id": 1, "company_number": "7015428", "company_name": "Padded Co"}]
        )

        result = ch_structural_ceiling(fixture)

        assert result["structurally_blocked_count"] == 0
        assert result["max_achievable_recovered"] == 1

    def test_finding_says_clears_not_still_below_when_the_ceiling_passes(self, tmp_path):
        """GIVEN a fixture where every control has a Company row (no
        structurally blocked rows -- the ceiling equals the full battery
        size, a hypothetical future state e.g. after a general NF-alias fix,
        spec A2.10.3)
        WHEN the structural ceiling is computed
        THEN ceiling_passes_gate is True AND `finding` says the ceiling
        "clears" the gate, never "still below" -- regression test for the
        exact self-contradiction an independent review caught ("Maximum
        achievable score is 12/12 (100.0%), still below the 90% gate
        (PASSES)") when the two halves of that sentence were not derived
        from the same boolean."""
        Company.objects.create(company_number="10041931", company_name="X")
        fixture = _write_ch_fixture(
            tmp_path, [{"id": 1, "company_number": "10041931", "company_name": "X"}]
        )

        result = ch_structural_ceiling(fixture)

        assert result["ceiling_passes_gate"] is True
        assert "clears the 90% gate" in result["finding"]
        assert "still below" not in result["finding"]

    def test_finding_says_still_below_not_clears_when_the_ceiling_fails(self, tmp_path):
        """GIVEN a fixture where the ceiling cannot reach the 90% gate (1 of 2
        controls structurally blocked)
        WHEN the structural ceiling is computed
        THEN ceiling_passes_gate is False AND `finding` says the ceiling is
        "still below" the gate, never "clears" -- the other half of the same
        regression guard as the test above."""
        Company.objects.create(company_number="10041931", company_name="X")
        fixture = _write_ch_fixture(
            tmp_path,
            [
                {"id": 1, "company_number": "10041931", "company_name": "X"},
                {"id": 2, "company_number": "NF000001", "company_name": "Y"},
            ],
        )

        result = ch_structural_ceiling(fixture)

        assert result["ceiling_passes_gate"] is False
        assert "still below the 90% gate" in result["finding"]
        assert "clears" not in result["finding"]

    def test_ceiling_fails_the_gate_when_below_90_percent(self, tmp_path):
        """GIVEN a 12-control fixture where the maximum achievable recovery is
        10/12 (83.3%)
        WHEN the structural ceiling is computed
        THEN ceiling_passes_gate is False -- the absolute best case still
        fails the >=9/10 (90%) gate, so no amount of further ingestion can
        rescue this stratum."""
        for i in range(1, 11):
            Company.objects.create(company_number=f"{i:08d}", company_name=f"Company {i}")
        controls = [
            {"id": i, "company_number": f"{i:08d}", "company_name": f"Company {i}"}
            for i in range(1, 11)
        ]
        controls += [
            {"id": 11, "company_number": "NF000001", "company_name": "Legacy A"},
            {"id": 12, "company_number": "NF000002", "company_name": "Legacy B"},
        ]
        fixture = _write_ch_fixture(tmp_path, controls)

        result = ch_structural_ceiling(fixture)

        assert result["max_achievable_recovered"] == 10
        assert result["total_controls"] == 12
        assert result["max_achievable_pct"] == 83.3
        assert result["ceiling_passes_gate"] is False


class TestThresholdArithmeticTable:
    def test_covers_every_score_from_zero_to_total(self):
        """GIVEN a battery size of 12
        WHEN the threshold arithmetic table is built
        THEN it has exactly 13 rows, one per possible recovered count 0..12."""
        table = threshold_arithmetic_table(12)

        assert len(table) == 13
        assert [row["recovered"] for row in table] == list(range(13))

    def test_nine_of_twelve_fails_the_ninety_percent_gate(self):
        """GIVEN a battery size of 12 and the default 90% gate
        WHEN the threshold arithmetic table is built
        THEN the row for 9/12 (75.0%) reports passes_gate=False -- this is
        the exact reading spec amendment v2.10 named as a real error: "nine
        successes regardless of denominator" is NOT the same as clearing
        >=9/10 (90%)."""
        table = threshold_arithmetic_table(12, gate_fraction=GATE_FRACTION)

        row_9 = next(r for r in table if r["recovered"] == 9)
        assert row_9["pct"] == 75.0
        assert row_9["passes_gate"] is False

    def test_ten_of_twelve_also_fails_the_gate(self):
        """GIVEN a battery size of 12
        WHEN the threshold arithmetic table is built
        THEN the row for 10/12 (83.3%) -- the CH stratum's own structural
        ceiling -- also reports passes_gate=False."""
        table = threshold_arithmetic_table(12)

        row_10 = next(r for r in table if r["recovered"] == 10)
        assert row_10["pct"] == 83.3
        assert row_10["passes_gate"] is False

    def test_eleven_of_twelve_passes_the_gate(self):
        """GIVEN a battery size of 12
        WHEN the threshold arithmetic table is built
        THEN the row for 11/12 (91.7%) reports passes_gate=True -- the first
        score that actually clears >=9/10."""
        table = threshold_arithmetic_table(12)

        row_11 = next(r for r in table if r["recovered"] == 11)
        assert row_11["pct"] == 91.7
        assert row_11["passes_gate"] is True


def _freeze_state(
    *, code_commit: str = "abc123", graph_hash: str = "graphhash", manifest_hash: str = "manihash"
) -> GateFreezeState:
    return GateFreezeState(
        code_commit=code_commit,
        graph_hash=graph_hash,
        attestation_inclusive_hash="attesthash",
        manifest_hash=manifest_hash,
        measured_at="2026-08-04T00:00:00+00:00",
    )


def _strict_gate_entry(*, ingested: int, total: int) -> dict:
    return {
        "ingested": ingested,
        "explicitly_failed": 0,
        "not_attempted": total - ingested,
        "total": total,
        "failure_manifest_sample": [],
        "known_limits": [],
        "extra": {},
    }


class TestLoadCoverageGateMeasurements:
    def test_both_required_families_are_unmeasured_when_no_report_file_exists(self, tmp_path):
        """GIVEN no coverage-gate report file exists at the given path
        WHEN the coverage-gate family is loaded
        THEN both REQUIRED_COVERAGE_FAMILIES land in `unmeasured`, `measured`
        is empty, and the reason names measure_coverage_gate.py as the
        missing producer -- unknown must never be silently read as
        passing."""
        report_path = tmp_path / "coverage_gate.json"

        measured, unmeasured = _load_coverage_gate_measurements(report_path, _freeze_state())

        assert measured == {}
        assert set(unmeasured) == set(REQUIRED_COVERAGE_FAMILIES)
        assert "measure_coverage_gate.py" in unmeasured["companies_house_officer_roster"]

    def test_loads_both_families_from_a_report_bound_to_the_current_state(self, tmp_path):
        """GIVEN a coverage_gate.json report bound to the SAME code_commit/
        graph_hash/manifest_hash as the certificate being emitted, with a
        real strict_gate entry for both required families
        WHEN the coverage-gate family is loaded
        THEN both are reconstructed as real CoverageMeasurement objects
        carrying the report's own counts, and `unmeasured` is empty."""
        freeze_state = _freeze_state()
        report_path = tmp_path / "coverage_gate.json"
        report_path.write_text(
            json.dumps(
                {
                    "code_commit": freeze_state.code_commit,
                    "graph_hash": freeze_state.graph_hash,
                    "manifest_hash": freeze_state.manifest_hash,
                    "strict_gate": {
                        "companies_house_officer_roster": _strict_gate_entry(
                            ingested=12227, total=12227
                        ),
                        "commons_register": _strict_gate_entry(ingested=25, total=3415),
                    },
                }
            ),
            encoding="utf-8",
        )

        measured, unmeasured = _load_coverage_gate_measurements(report_path, freeze_state)

        assert unmeasured == {}
        assert measured["companies_house_officer_roster"].ingested == 12227
        assert measured["companies_house_officer_roster"].total == 12227
        assert measured["companies_house_officer_roster"].passed is True
        assert measured["commons_register"].ingested == 25
        assert measured["commons_register"].total == 3415
        assert measured["commons_register"].passed is False

    def test_treats_a_report_bound_to_a_different_graph_hash_as_unmeasured(self, tmp_path):
        """GIVEN a coverage_gate.json report file that exists but was measured
        against a DIFFERENT graph_hash than the certificate being emitted
        WHEN the coverage-gate family is loaded
        THEN both required families are unmeasured with a reason citing the
        state mismatch -- a stale coverage measurement must never be
        silently trusted for a different graph state (spec A2.4.5)."""
        report_path = tmp_path / "coverage_gate.json"
        report_path.write_text(
            json.dumps(
                {
                    "code_commit": "abc123",
                    "graph_hash": "a-different-graph-hash",
                    "manifest_hash": "manihash",
                    "strict_gate": {
                        "companies_house_officer_roster": _strict_gate_entry(
                            ingested=12227, total=12227
                        ),
                        "commons_register": _strict_gate_entry(ingested=3415, total=3415),
                    },
                }
            ),
            encoding="utf-8",
        )

        measured, unmeasured = _load_coverage_gate_measurements(report_path, _freeze_state())

        assert measured == {}
        assert set(unmeasured) == set(REQUIRED_COVERAGE_FAMILIES)
        assert "different code/graph/manifest state" in unmeasured["commons_register"]

    def test_a_bound_report_missing_one_family_only_marks_that_family_unmeasured(self, tmp_path):
        """GIVEN a bound report whose strict_gate carries only
        'companies_house_officer_roster', not 'commons_register'
        WHEN the coverage-gate family is loaded
        THEN 'companies_house_officer_roster' is measured and
        'commons_register' alone is unmeasured -- a partial report never
        silently credits the family it does not carry."""
        freeze_state = _freeze_state()
        report_path = tmp_path / "coverage_gate.json"
        report_path.write_text(
            json.dumps(
                {
                    "code_commit": freeze_state.code_commit,
                    "graph_hash": freeze_state.graph_hash,
                    "manifest_hash": freeze_state.manifest_hash,
                    "strict_gate": {
                        "companies_house_officer_roster": _strict_gate_entry(
                            ingested=12227, total=12227
                        ),
                    },
                }
            ),
            encoding="utf-8",
        )

        measured, unmeasured = _load_coverage_gate_measurements(report_path, freeze_state)

        assert "companies_house_officer_roster" in measured
        assert set(unmeasured) == {"commons_register"}


class TestCoverageGateNote:
    def test_note_names_both_families_unmeasured_with_their_reasons(self):
        """GIVEN both coverage families are unmeasured
        WHEN the note is composed
        THEN it says UNMEASURED for both family names and includes each
        reason string verbatim."""
        note = coverage_gate_note(
            {},
            {
                "companies_house_officer_roster": "no report found",
                "commons_register": "no report found",
            },
        )

        assert "companies_house_officer_roster: UNMEASURED (no report found)" in note
        assert "commons_register: UNMEASURED (no report found)" in note

    def test_note_reports_a_measured_pass_and_a_measured_fail_accurately(self):
        """GIVEN one coverage family measured fully accounted for (PASS) and
        the other measured with a partial shortfall (FAIL)
        WHEN the note is composed
        THEN the prose says PASS for the passing family and FAIL for the
        failing one, each with its real accounted-for/total counts --
        proving the note is derived from the measured booleans, never a
        fixed claim (the exact prose/data-disagreement defect class an
        earlier review caught in this same file's ch_structural_ceiling)."""
        measurements = {
            "companies_house_officer_roster": CoverageMeasurement(
                name="companies_house_officer_roster",
                ingested=12227,
                explicitly_failed=0,
                not_attempted=0,
                total=12227,
            ),
            "commons_register": CoverageMeasurement(
                name="commons_register",
                ingested=25,
                explicitly_failed=0,
                not_attempted=3390,
                total=3415,
            ),
        }

        note = coverage_gate_note(measurements, {})

        assert "companies_house_officer_roster: 12227/12227 accounted for -- PASS" in note
        assert "commons_register: 25/3415 accounted for -- FAIL" in note


class TestManifestHashOrUnavailable:
    def test_returns_the_real_sha256_when_the_manifest_file_exists(self, tmp_path):
        """GIVEN a real manifest file on disk
        WHEN the manifest hash is resolved
        THEN it returns the file's actual sha256 hex digest, matching
        `compute_manifest_hash`'s own algorithm."""
        manifest_path = tmp_path / "gold_manifest.csv"
        manifest_path.write_bytes(b"case_id,company_number\n1,00000001\n")

        result = _manifest_hash_or_unavailable(manifest_path)

        assert result == hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    def test_returns_an_explicit_unavailable_marker_when_the_file_is_missing(self, tmp_path):
        """GIVEN no manifest file at the given path
        WHEN the manifest hash is resolved
        THEN it returns a string that says UNAVAILABLE rather than raising or
        fabricating a hash -- a missing input must be an auditable statement,
        never a silent gap (ADR-008)."""
        missing_path = tmp_path / "does_not_exist.csv"

        result = _manifest_hash_or_unavailable(missing_path)

        assert result.startswith("UNAVAILABLE:")


class TestSealedCohortStatement:
    def _ceiling(self, *, passes: bool) -> dict:
        return {
            "max_achievable_recovered": 12 if passes else 10,
            "total_controls": 12,
            "max_achievable_pct": 100.0 if passes else 83.3,
            "ceiling_passes_gate": passes,
        }

    def test_says_cannot_reach_when_the_ceiling_fails(self):
        """GIVEN a ceiling that cannot reach the gate (today's real
        Companies House shape) and a certificate whose blockers include the
        CH stratum
        WHEN the sealed-cohort statement is composed
        THEN it says the ceiling "cannot reach" the readiness gate, never
        "clears" it."""
        ceiling = self._ceiling(passes=False)
        certificate = {"blockers": [{"gate": "stratum:ch_officer_appointment"}]}

        statement = sealed_cohort_statement(ceiling, certificate)

        assert "cannot reach the 90% readiness gate" in statement
        assert "clears" not in statement

    def test_says_clears_when_the_ceiling_passes(self):
        """GIVEN a ceiling that DOES clear the gate (a hypothetical future
        state after a general NF-alias fix, spec A2.10.3) but the
        certificate still has OTHER blockers (Commons, Lords)
        WHEN the sealed-cohort statement is composed
        THEN it says the CH ceiling "clears" the gate and does not block
        scoring by itself -- it must not keep asserting CH "cannot reach"
        the gate once that stops being true, even though the cohort is
        still, correctly, not scored because another stratum still fails.
        This is the direct regression test for the bug an independent
        review caught by swapping the two NF rows for a resolvable number."""
        ceiling = self._ceiling(passes=True)
        certificate = {"blockers": [{"gate": "stratum:commons_declared_interest"}]}

        statement = sealed_cohort_statement(ceiling, certificate)

        assert "clears the 90% readiness gate" in statement
        assert "cannot reach" not in statement
        assert "stratum:commons_declared_interest" in statement

    def test_names_every_blocking_gate(self):
        """GIVEN a certificate with three blockers
        WHEN the sealed-cohort statement is composed
        THEN all three gate names appear in the statement -- a reader should
        never have to cross-reference `blockers` separately to know what
        blocked scoring."""
        ceiling = self._ceiling(passes=False)
        certificate = {
            "blockers": [
                {"gate": "stratum:commons_declared_interest"},
                {"gate": "stratum:lords_declared_interest"},
                {"gate": "stratum:ch_officer_appointment"},
            ]
        }

        statement = sealed_cohort_statement(ceiling, certificate)

        assert "stratum:commons_declared_interest" in statement
        assert "stratum:lords_declared_interest" in statement
        assert "stratum:ch_officer_appointment" in statement


class TestElectoralCommissionMaterialityNote:
    def _passing_ec_measurement(self) -> StratumMeasurement:
        return StratumMeasurement(
            name="electoral_commission",
            available=True,
            retrieval_recovered=11,
            retrieval_total=12,
            temporal_recovered=11,
            temporal_total=12,
        )

    def test_flags_ec_as_not_material_even_when_passing(self):
        """GIVEN an Electoral Commission measurement that passed (11/12, the
        real measured shape)
        WHEN the materiality note is built
        THEN in_material_strata is False -- EC is not in
        run_gold_benchmark.MATERIAL_STRATA regardless of its own score, and
        the note carries the real recovered/total/passed values."""
        note = electoral_commission_materiality_note(self._passing_ec_measurement())

        assert note["in_material_strata"] is False
        assert note["passed"] is True
        assert note["measured_recovered"] == 11
        assert note["measured_total"] == 12

    def test_caveat_names_the_one_of_four_vs_zero_of_three_framing(self):
        """GIVEN a passing EC measurement
        WHEN the materiality note is built
        THEN the caveat text explicitly contrasts '1 of 4' against '0 of 3
        material gates' -- the exact misreading this note exists to
        prevent."""
        note = electoral_commission_materiality_note(self._passing_ec_measurement())

        assert "1 of 4" in note["caveat"]
        assert "0 of 3 material gates" in note["caveat"]


@pytest.mark.django_db
class TestMainRefusesToEmitWhenEveryMeasuredStratumPasses:
    def test_raises_system_exit_and_writes_nothing(self, tmp_path, monkeypatch):
        """GIVEN measure_all_strata reports every stratum passing (a
        hypothetical state that contradicts spec amendment v2.10's own
        finding that Companies House fails)
        WHEN main() runs
        THEN it raises SystemExit naming the refusal, and no output file is
        written -- this script must never silently disagree with the
        pre-registered finding by emitting nothing, or emit a certificate
        claiming success."""
        import scripts.emit_no_score_certificate as emit_mod

        passing = StratumMeasurement(
            name="ch_officer_appointment",
            available=True,
            retrieval_recovered=12,
            retrieval_total=12,
            temporal_recovered=12,
            temporal_total=12,
        )
        monkeypatch.setattr(
            emit_mod, "measure_all_strata", lambda **kwargs: {"ch_officer_appointment": passing}
        )
        out_path = tmp_path / "out.json"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "emit_no_score_certificate.py",
                "--out",
                str(out_path),
                "--manifest",
                str(tmp_path / "no_such_manifest.csv"),
            ],
        )

        with pytest.raises(SystemExit, match="REFUSING to emit"):
            emit_mod.main()

        assert not out_path.exists()


def _failing_strata_shape() -> dict[str, StratumMeasurement]:
    """The real measured spec-v2.10 shape: CH/Commons/Lords fail, Electoral
    Commission passes but is non-material. Mirrors the committed
    `experiments/no_score_certificate.json` so these integration tests
    exercise a realistic combined certificate, not a contrived one."""
    return {
        "ch_officer_appointment": StratumMeasurement(
            name="ch_officer_appointment",
            available=True,
            retrieval_recovered=7,
            retrieval_total=12,
            temporal_recovered=7,
            temporal_total=12,
        ),
        "commons_declared_interest": StratumMeasurement(name="commons_declared_interest"),
        "lords_declared_interest": StratumMeasurement(name="lords_declared_interest"),
        "electoral_commission": StratumMeasurement(
            name="electoral_commission",
            available=True,
            retrieval_recovered=11,
            retrieval_total=12,
            temporal_recovered=11,
            temporal_total=12,
        ),
    }


@pytest.mark.django_db
class TestMainCoverageGateFamily:
    def test_adds_unmeasured_coverage_blockers_when_no_coverage_report_exists(
        self, tmp_path, monkeypatch
    ):
        """GIVEN the real measured stratum shape (CH/Commons/Lords fail) and
        NO experiments/coverage_gate.json report anywhere on disk -- the
        actual state of this repository, since scripts/measure_coverage_gate.py
        has never been run here
        WHEN main() runs
        THEN the written certificate's blockers include the three stratum
        blockers PLUS both required coverage-gate families, each reasoned
        UNMEASURED -- the certificate can no longer emit with the
        coverage-gate family silently absent, and the verdict is unchanged."""
        import scripts.emit_no_score_certificate as emit_mod

        monkeypatch.setattr(
            emit_mod, "measure_all_strata", lambda **kwargs: _failing_strata_shape()
        )
        out_path = tmp_path / "out.json"
        missing_report = tmp_path / "no_such_coverage_gate.json"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "emit_no_score_certificate.py",
                "--out",
                str(out_path),
                "--manifest",
                str(tmp_path / "no_such_manifest.csv"),
                "--coverage-gate-report",
                str(missing_report),
            ],
        )

        emit_mod.main()

        certificate = json.loads(out_path.read_text(encoding="utf-8"))
        gates_named = {b["gate"] for b in certificate["blockers"]}
        assert gates_named == {
            "stratum:ch_officer_appointment",
            "stratum:commons_declared_interest",
            "stratum:lords_declared_interest",
            "coverage:companies_house_officer_roster",
            "coverage:commons_register",
        }
        coverage_blockers = {
            b["gate"]: b for b in certificate["blockers"] if b["gate"].startswith("coverage:")
        }
        assert coverage_blockers["coverage:companies_house_officer_roster"]["reason"].startswith(
            "UNMEASURED:"
        )
        assert coverage_blockers["coverage:commons_register"]["reason"].startswith("UNMEASURED:")
        assert certificate["verdict"] == "NO SCORE -- INSTRUMENT-LIMITED"
        assert set(certificate["coverage_gate_unmeasured"]) == set(REQUIRED_COVERAGE_FAMILIES)
        assert "UNMEASURED" in certificate["note"]

    def test_omits_coverage_blockers_when_a_bound_report_shows_full_coverage(
        self, tmp_path, monkeypatch
    ):
        """GIVEN the real measured stratum shape (CH/Commons/Lords fail) and a
        coverage_gate.json report BOUND to this exact run's code_commit/
        graph_hash/manifest_hash, showing both required coverage families
        fully accounted for
        WHEN main() runs
        THEN the written certificate's blockers name ONLY the three stratum
        gates -- no coverage:* entry -- because a real, bound, passing
        measurement is not a blocker, and coverage_gate_measured records
        both families as passed."""
        import scripts.emit_no_score_certificate as emit_mod

        monkeypatch.setattr(
            emit_mod, "measure_all_strata", lambda **kwargs: _failing_strata_shape()
        )
        monkeypatch.setattr(emit_mod, "current_code_commit", lambda: "fixedcommit123")
        monkeypatch.setattr(emit_mod, "compute_graph_hash", lambda: "fixedgraphhash456")

        manifest_path = tmp_path / "gold_manifest.csv"
        manifest_path.write_bytes(b"case_id,company_number\n1,00000001\n")
        manifest_hash = compute_manifest_hash(manifest_path)

        report_path = tmp_path / "coverage_gate.json"
        report_path.write_text(
            json.dumps(
                {
                    "code_commit": "fixedcommit123",
                    "graph_hash": "fixedgraphhash456",
                    "manifest_hash": manifest_hash,
                    "strict_gate": {
                        "companies_house_officer_roster": _strict_gate_entry(
                            ingested=12227, total=12227
                        ),
                        "commons_register": _strict_gate_entry(ingested=3415, total=3415),
                    },
                }
            ),
            encoding="utf-8",
        )
        out_path = tmp_path / "out.json"
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "emit_no_score_certificate.py",
                "--out",
                str(out_path),
                "--manifest",
                str(manifest_path),
                "--coverage-gate-report",
                str(report_path),
            ],
        )

        emit_mod.main()

        certificate = json.loads(out_path.read_text(encoding="utf-8"))
        gates_named = {b["gate"] for b in certificate["blockers"]}
        assert gates_named == {
            "stratum:ch_officer_appointment",
            "stratum:commons_declared_interest",
            "stratum:lords_declared_interest",
        }
        assert certificate["coverage_gate_measured"]["companies_house_officer_roster"]["passed"]
        assert certificate["coverage_gate_measured"]["commons_register"]["passed"]
        assert certificate["coverage_gate_unmeasured"] == {}
        assert certificate["verdict"] == "NO SCORE -- INSTRUMENT-LIMITED"
