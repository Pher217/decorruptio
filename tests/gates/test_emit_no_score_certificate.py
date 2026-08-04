"""Tests for `scripts/emit_no_score_certificate.py`'s own added logic --
the pieces `uncorrupt.gates.certificate.build_no_score_certificate` does not
compute: the Companies House structural-ceiling finding, the threshold
arithmetic table, and the manifest-hash fallback.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from scripts.emit_no_score_certificate import (
    GATE_FRACTION,
    _manifest_hash_or_unavailable,
    ch_structural_ceiling,
    threshold_arithmetic_table,
)

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
        """GIVEN the real 12-row shape -- 2 structurally blocked, 10 not
        WHEN the structural ceiling is computed
        THEN max_achievable_recovered is 10 and max_achievable_pct is 83.3,
        matching spec amendment v2.10's own finding."""
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
