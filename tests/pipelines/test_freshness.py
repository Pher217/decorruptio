"""Tests for the freshness SLA checker + staleness labels."""

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from uncorrupt.pipelines.freshness import (
    _compute_label,
    check_freshness,
    check_freshness_for_source,
)
from uncorrupt.staging.models import IngestRun


def _write_source_yaml(tmp_path: Path, source_id: str, sla_days: int) -> Path:
    """Write a source YAML file and return the sources directory."""
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
    rows: int = 100,
) -> IngestRun:
    return IngestRun.objects.create(
        source_id=source_id,
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        rows_ingested=rows,
    )


class TestComputeLabel:
    """Test the _compute_label function directly."""

    def test_no_ingest_is_critical(self):
        assert _compute_label(None, 7) == "critical"

    def test_within_sla_is_fresh(self):
        assert _compute_label(3, 7) == "fresh"

    def test_exactly_at_sla_is_fresh(self):
        assert _compute_label(7, 7) == "fresh"

    def test_over_sla_but_under_2x_is_stale(self):
        assert _compute_label(10, 7) == "stale"

    def test_exactly_2x_sla_is_stale(self):
        assert _compute_label(14, 7) == "stale"

    def test_over_2x_sla_is_critical(self):
        assert _compute_label(15, 7) == "critical"


@pytest.mark.django_db
class TestCheckFreshnessForSource:
    """Test the single-source freshness checker."""

    def test_fresh_source(self):
        """A source with a recent successful ingest is fresh."""
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        _create_ingest_run(
            "ec_donations",
            now - timedelta(hours=2),
            now - timedelta(hours=1),
        )
        status = check_freshness_for_source("ec_donations", sla_days=7, now=now)
        assert status.label == "fresh"
        assert status.is_within_sla is True
        assert status.days_since_success is not None
        assert status.days_since_success < 1.0

    def test_stale_source(self):
        """A source with an ingest older than SLA but under 2x is stale."""
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        _create_ingest_run(
            "ec_donations",
            now - timedelta(days=10),
            now - timedelta(days=10, hours=-1),
        )
        status = check_freshness_for_source("ec_donations", sla_days=7, now=now)
        assert status.label == "stale"
        assert status.is_within_sla is False

    def test_critical_source(self):
        """A source with an ingest older than 2x SLA is critical."""
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        _create_ingest_run(
            "ec_donations",
            now - timedelta(days=20),
            now - timedelta(days=20, hours=-1),
        )
        status = check_freshness_for_source("ec_donations", sla_days=7, now=now)
        assert status.label == "critical"
        assert status.is_within_sla is False

    def test_no_successful_ingest_is_critical(self):
        """A source with no successful ingest is critical."""
        status = check_freshness_for_source("ec_donations", sla_days=7)
        assert status.label == "critical"
        assert status.days_since_success is None

    def test_failed_ingest_does_not_count(self):
        """A failed ingest does not update freshness."""
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        IngestRun.objects.create(
            source_id="ec_donations",
            started_at=now - timedelta(hours=1),
            finished_at=now - timedelta(minutes=30),
            status="failed",
        )
        status = check_freshness_for_source("ec_donations", sla_days=7, now=now)
        assert status.label == "critical"  # No *successful* ingest

    def test_most_recent_success_is_used(self):
        """When multiple successful ingests exist, the most recent is used."""
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        _create_ingest_run(
            "ec_donations",
            now - timedelta(days=20),
            now - timedelta(days=20, hours=-1),
        )
        _create_ingest_run(
            "ec_donations",
            now - timedelta(hours=2),
            now - timedelta(hours=1),
        )
        status = check_freshness_for_source("ec_donations", sla_days=7, now=now)
        assert status.label == "fresh"


@pytest.mark.django_db
class TestCheckFreshnessAll:
    """Test the multi-source freshness checker."""

    def test_multiple_sources(self, tmp_path):
        """Check freshness across multiple registered sources."""
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        sources_dir = _write_source_yaml(tmp_path, "ec_donations", 7)
        _write_source_yaml(tmp_path, "uk_contracts_finder", 30)

        _create_ingest_run(
            "ec_donations",
            now - timedelta(days=1, hours=1),
            now - timedelta(days=1),
        )

        results = check_freshness(sources_dir=sources_dir, now=now)
        assert len(results) == 2

        assert results[0].source_id == "ec_donations"
        assert results[0].label == "fresh"
        assert results[1].source_id == "uk_contracts_finder"
        assert results[1].label == "critical"

    def test_no_sources_dir_returns_empty(self, tmp_path):
        """A non-existent sources directory returns empty list."""
        results = check_freshness(sources_dir=tmp_path / "nonexistent")
        assert results == []

    def test_mixed_freshness(self, tmp_path):
        """Sources with varying freshness get correct labels."""
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        sources_dir = _write_source_yaml(tmp_path, "source_a", 7)
        _write_source_yaml(tmp_path, "source_b", 7)
        _write_source_yaml(tmp_path, "source_c", 7)

        _create_ingest_run(
            "source_a",
            now - timedelta(days=2, hours=1),
            now - timedelta(days=2),
        )
        _create_ingest_run(
            "source_b",
            now - timedelta(days=10, hours=1),
            now - timedelta(days=10),
        )

        results = check_freshness(sources_dir=sources_dir, now=now)

        labels = {r.source_id: r.label for r in results}
        assert labels["source_a"] == "fresh"
        assert labels["source_b"] == "stale"
        assert labels["source_c"] == "critical"
