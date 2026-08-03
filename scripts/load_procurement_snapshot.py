"""Load the frozen UK Contracts Finder OCDS bulk snapshot into staging.

Streams the gzip-compressed JSONL release files under
experiments/snapshot_uk_covid_2020/{year}.jsonl.gz — one OCDS release per
line — into Tender/Award via the existing uk_contracts_finder mapping
(uncorrupt.staging.ingest.ingest_artifacts). Idempotent: update_or_create on
the natural key (source_id, tender_id[, award_id]), so re-running never
duplicates rows.

Usage:
    uv run python scripts/load_procurement_snapshot.py
    uv run python scripts/load_procurement_snapshot.py --year 2020
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.connectors.base import RawArtifact
from uncorrupt.staging.ingest import ingest_artifacts
from uncorrupt.staging.models import IngestRun

SOURCE_ID = "uk_contracts_finder"
SNAPSHOT_DIR = Path("experiments/snapshot_uk_covid_2020")
YEARS = ("2020", "2021", "2022", "2023")
BATCH_SIZE = 500
DEFAULT_PROGRESS_EVERY = 2000


def _load_file_provenance(snapshot_dir: Path, year: str) -> dict[str, Any]:
    """Read the recorded content hash + download URL for one year's file."""
    prov_path = snapshot_dir / "bulk_provenance.json"
    if not prov_path.exists():
        return {}
    provenance = json.loads(prov_path.read_text())
    result: dict[str, Any] = provenance.get("files", {}).get(year, {})
    return result


def load_year(
    year: str,
    snapshot_dir: Path = SNAPSHOT_DIR,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
) -> dict[str, int]:
    """Stream one year's jsonl.gz file into Tender/Award. Returns {seen, ingested, malformed}."""
    path = snapshot_dir / f"{year}.jsonl.gz"
    if not path.exists():
        raise FileNotFoundError(f"Snapshot file not found: {path}")

    file_provenance = _load_file_provenance(snapshot_dir, year)
    content_hash = file_provenance.get("sha256")
    download_url = file_provenance.get("download_url", str(path))

    run = IngestRun.objects.create(
        source_id=SOURCE_ID,
        started_at=datetime.now(UTC),
        status="running",
        content_hash=content_hash,
    )

    seen = 0
    ingested = 0
    malformed = 0
    batch: list[RawArtifact] = []

    def flush() -> None:
        nonlocal ingested, malformed
        if not batch:
            return
        count = ingest_artifacts(SOURCE_ID, batch)
        ingested += count
        malformed += len(batch) - count
        batch.clear()

    print(f"[{year}] loading {path} ...", flush=True)
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line:
                continue
            seen += 1
            try:
                release = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue

            batch.append(
                RawArtifact(
                    payload=json.dumps(release, ensure_ascii=False).encode(),
                    source_url=download_url,
                    media_type="application/json",
                )
            )
            if len(batch) >= BATCH_SIZE:
                flush()

            if seen % progress_every == 0:
                print(
                    f"[{year}] {seen} read, {ingested} ingested, {malformed} malformed",
                    flush=True,
                )

    flush()

    run.finished_at = datetime.now(UTC)
    run.status = "success"
    run.rows_ingested = ingested
    run.save(update_fields=["finished_at", "status", "rows_ingested"])

    print(f"[{year}] done: {seen} read, {ingested} ingested, {malformed} malformed", flush=True)
    return {"seen": seen, "ingested": ingested, "malformed": malformed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--year", choices=YEARS, default=None, help="Load a single year; default loads all."
    )
    parser.add_argument("--snapshot-dir", default=str(SNAPSHOT_DIR))
    parser.add_argument("--progress-every", type=int, default=DEFAULT_PROGRESS_EVERY)
    args = parser.parse_args()

    snapshot_dir = Path(args.snapshot_dir)
    years = [args.year] if args.year else list(YEARS)

    totals = {"seen": 0, "ingested": 0, "malformed": 0}
    for year in years:
        stats = load_year(year, snapshot_dir, args.progress_every)
        for key in totals:
            totals[key] += stats[key]

    print(
        f"\nTOTAL: {totals['seen']} read, {totals['ingested']} ingested, "
        f"{totals['malformed']} malformed"
    )


if __name__ == "__main__":
    main()
