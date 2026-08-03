"""Tests for the refresh-cadence declarative view (schedules.py)."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from uncorrupt.pipelines.schedules import refresh_schedule
from uncorrupt.staging.models import IngestRun


def _write_source_yaml(tmp_path: Path, source_id: str, sla_days: int) -> Path:
    sources_dir = tmp_path / "sources"
    sources_dir.mkdir(exist_ok=True)
    yml_path = sources_dir / f"{source_id}.yml"
    yml_path.write_text(
        yaml.dump(
            {
                "source_id": source_id,
                "name": f"Test Source {source_id}",
                "freshness_sla_days": sla_days,
            }
        )
    )
    return sources_dir


def _create_ingest_run(
    source_id: str,
    started_at: datetime,
    finished_at: datetime,
    status: str = "success",
) -> IngestRun:
    return IngestRun.objects.create(
        source_id=source_id,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        rows_ingested=100,
    )


@pytest.mark.django_db
class TestRefreshSchedule:
    """refresh_schedule answers 'what is due, and what is overdue'."""

    def test_fresh_source_is_neither_due_nor_overdue(self, tmp_path):
        """GIVEN a source ingested well within its SLA
        WHEN computing the refresh schedule
        THEN it appears in neither `due` nor `overdue`.
        """
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        sources_dir = _write_source_yaml(tmp_path, "fresh_source", 7)
        _create_ingest_run(
            "fresh_source",
            now - timedelta(hours=2),
            now - timedelta(hours=1),
        )

        cadence = refresh_schedule(sources_dir=sources_dir, now=now)

        assert "fresh_source" not in {s.source_id for s in cadence.due}
        assert "fresh_source" not in {s.source_id for s in cadence.overdue}

    def test_overdue_source_appears_in_due_for_refresh_list(self, tmp_path):
        """GIVEN a source last ingested well beyond 2x its SLA (critical)
        WHEN computing the refresh schedule
        THEN it appears in BOTH `due` and `overdue`.
        """
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        sources_dir = _write_source_yaml(tmp_path, "overdue_source", 7)
        _create_ingest_run(
            "overdue_source",
            now - timedelta(days=20),
            now - timedelta(days=20, hours=-1),
        )

        cadence = refresh_schedule(sources_dir=sources_dir, now=now)

        assert "overdue_source" in {s.source_id for s in cadence.due}
        assert "overdue_source" in {s.source_id for s in cadence.overdue}

    def test_stale_source_is_due_and_overdue(self, tmp_path):
        """GIVEN a source past its SLA but under 2x (stale)
        WHEN computing the refresh schedule
        THEN it appears in both `due` and `overdue` (stale already breached
        the SLA window once, even if not yet critical).
        """
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        sources_dir = _write_source_yaml(tmp_path, "stale_source", 7)
        _create_ingest_run(
            "stale_source",
            now - timedelta(days=10),
            now - timedelta(days=10, hours=-1),
        )

        cadence = refresh_schedule(sources_dir=sources_dir, now=now)

        assert "stale_source" in {s.source_id for s in cadence.due}
        assert "stale_source" in {s.source_id for s in cadence.overdue}

    def test_never_run_source_is_due_but_not_overdue(self, tmp_path):
        """GIVEN a source with no IngestRun row at all (never attempted --
        "unknown")
        WHEN computing the refresh schedule
        THEN it appears in `due` (needs a first run) but NOT in `overdue`
        (there is no prior success for it to have lapsed).
        """
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        sources_dir = _write_source_yaml(tmp_path, "never_run_source", 7)

        cadence = refresh_schedule(sources_dir=sources_dir, now=now)

        assert "never_run_source" in {s.source_id for s in cadence.due}
        assert "never_run_source" not in {s.source_id for s in cadence.overdue}

    def test_overdue_is_always_a_subset_of_due(self, tmp_path):
        """GIVEN a mix of fresh, stale, critical, and never-run sources
        WHEN computing the refresh schedule
        THEN every overdue source id is also a due source id.
        """
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        sources_dir = _write_source_yaml(tmp_path, "fresh_source", 7)
        _write_source_yaml(tmp_path, "stale_source", 7)
        _write_source_yaml(tmp_path, "critical_source", 7)
        _write_source_yaml(tmp_path, "never_run_source", 7)

        _create_ingest_run("fresh_source", now - timedelta(hours=2), now - timedelta(hours=1))
        _create_ingest_run(
            "stale_source",
            now - timedelta(days=10),
            now - timedelta(days=10, hours=-1),
        )
        _create_ingest_run(
            "critical_source",
            now - timedelta(days=20),
            now - timedelta(days=20, hours=-1),
        )

        cadence = refresh_schedule(sources_dir=sources_dir, now=now)

        due_ids = {s.source_id for s in cadence.due}
        overdue_ids = {s.source_id for s in cadence.overdue}
        assert overdue_ids.issubset(due_ids)
        assert overdue_ids == {"stale_source", "critical_source"}
        assert due_ids == {"stale_source", "critical_source", "never_run_source"}
