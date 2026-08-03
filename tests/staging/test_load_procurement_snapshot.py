"""Tests for the UK procurement snapshot loader.

Covers idempotency (re-running never duplicates), integer-cents money
handling, and malformed-record skipping (counted, never crashes the run).
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from scripts.load_procurement_snapshot import load_year

from uncorrupt.staging.models import Award, IngestRun, Tender

RELEASE_A = {
    "ocid": "ocds-test-0001",
    "tender": {
        "id": "tender-0001",
        "title": "Test tender A",
        "status": "complete",
        "value": {"amount": 1234.56, "currency": "GBP"},
    },
    "awards": [
        {
            "id": "award-0001",
            "status": "active",
            "value": {"amount": 1234.56, "currency": "GBP"},
            "suppliers": [{"id": "GB-CFS-1", "name": "Acme Ltd"}],
        }
    ],
    "parties": [
        {
            "id": "GB-CFS-1",
            "name": "Acme Ltd",
            "roles": ["supplier"],
            "identifier": {"scheme": "GB-COH", "id": "01234567"},
        }
    ],
}

RELEASE_B = {
    "ocid": "ocds-test-0002",
    "tender": {"id": "tender-0002", "title": "Test tender B", "status": "planned"},
    "awards": [],
    "parties": [],
}


def _write_snapshot(tmp_path: Path, year: str, lines: list[str]) -> Path:
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir(exist_ok=True)
    path = snapshot_dir / f"{year}.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for line in lines:
            f.write(line + "\n")
    return snapshot_dir


@pytest.mark.django_db
class TestLoadYear:
    def test_loads_valid_releases_into_tender_and_award(self, tmp_path):
        """GIVEN a snapshot file with two valid OCDS releases
        WHEN load_year runs
        THEN both tenders and the one award are created."""
        lines = [json.dumps(RELEASE_A), json.dumps(RELEASE_B)]
        snapshot_dir = _write_snapshot(tmp_path, "2020", lines)

        stats = load_year("2020", snapshot_dir=snapshot_dir, progress_every=1000)

        assert stats == {"seen": 2, "ingested": 2, "malformed": 0}
        assert Tender.objects.filter(source_id="uk_contracts_finder").count() == 2
        assert Award.objects.filter(source_id="uk_contracts_finder").count() == 1

    def test_money_stored_as_integer_cents(self, tmp_path):
        """GIVEN a release with a value of 1234.56 GBP
        WHEN load_year runs
        THEN value_amount_cents stores exactly 123456 (integer cents, no float)."""
        snapshot_dir = _write_snapshot(tmp_path, "2020", [json.dumps(RELEASE_A)])

        load_year("2020", snapshot_dir=snapshot_dir, progress_every=1000)

        tender = Tender.objects.get(source_id="uk_contracts_finder", tender_id="tender-0001")
        award = Award.objects.get(source_id="uk_contracts_finder", tender_id="tender-0001")
        assert tender.value_amount_cents == 123456
        assert award.value_amount_cents == 123456
        assert isinstance(tender.value_amount_cents, int)

    def test_rerun_is_idempotent(self, tmp_path):
        """GIVEN a snapshot already loaded once
        WHEN load_year runs again on the same file
        THEN no duplicate Tender/Award rows are created."""
        snapshot_dir = _write_snapshot(tmp_path, "2020", [json.dumps(RELEASE_A)])

        load_year("2020", snapshot_dir=snapshot_dir, progress_every=1000)
        load_year("2020", snapshot_dir=snapshot_dir, progress_every=1000)

        assert Tender.objects.filter(source_id="uk_contracts_finder").count() == 1
        assert Award.objects.filter(source_id="uk_contracts_finder").count() == 1

    def test_malformed_json_line_is_skipped_and_counted(self, tmp_path):
        """GIVEN a snapshot file with one valid release and one malformed JSON line
        WHEN load_year runs
        THEN the valid release is ingested, the malformed line is counted, and the
        run does not crash."""
        lines = [json.dumps(RELEASE_A), "{not valid json"]
        snapshot_dir = _write_snapshot(tmp_path, "2020", lines)

        stats = load_year("2020", snapshot_dir=snapshot_dir, progress_every=1000)

        assert stats == {"seen": 2, "ingested": 1, "malformed": 1}
        assert Tender.objects.filter(source_id="uk_contracts_finder").count() == 1

    def test_missing_file_raises(self, tmp_path):
        """GIVEN a snapshot directory with no file for the requested year
        WHEN load_year runs
        THEN it raises FileNotFoundError rather than silently doing nothing."""
        snapshot_dir = tmp_path / "empty"
        snapshot_dir.mkdir()

        with pytest.raises(FileNotFoundError):
            load_year("2020", snapshot_dir=snapshot_dir, progress_every=1000)

    def test_records_ingest_run_with_provenance(self, tmp_path):
        """GIVEN a snapshot with provenance recorded in bulk_provenance.json
        WHEN load_year runs
        THEN an IngestRun row is created carrying the content hash and row count."""
        snapshot_dir = _write_snapshot(tmp_path, "2020", [json.dumps(RELEASE_A)])
        provenance = {
            "files": {
                "2020": {
                    "sha256": "deadbeef",
                    "download_url": "https://example.test/2020.jsonl.gz",
                }
            }
        }
        (snapshot_dir / "bulk_provenance.json").write_text(json.dumps(provenance))

        load_year("2020", snapshot_dir=snapshot_dir, progress_every=1000)

        run = IngestRun.objects.get(source_id="uk_contracts_finder")
        assert run.status == "success"
        assert run.content_hash == "deadbeef"
        assert run.rows_ingested == 1
