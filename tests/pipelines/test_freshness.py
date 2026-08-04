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
from uncorrupt.pipelines.run_recorder import Completeness, record_ingest_run
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

    def test_no_ingest_and_never_run_is_unknown(self):
        """GIVEN no days-since-success value AND has_any_run=False
        WHEN computing the label
        THEN it is reported as "unknown" (never attempted), not "critical"
        (attempted and failed) — the distinction this pass exists to add.
        """
        assert _compute_label(None, 7, has_any_run=False) == "unknown"

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

    def test_never_run_is_unknown(self):
        """GIVEN a source with no IngestRun row at all (never attempted)
        WHEN checking its freshness
        THEN the label is "unknown", not "critical" -- "critical" is
        reserved for a source that HAS been attempted (an IngestRun row
        exists) but never succeeded, or succeeded too long ago.
        """
        status = check_freshness_for_source("ec_donations", sla_days=7)
        assert status.label == "unknown"
        assert status.days_since_success is None

    def test_attempted_but_always_failed_is_critical(self):
        """GIVEN a source with only failed IngestRun rows (attempted, no success)
        WHEN checking its freshness
        THEN the label is "critical", distinct from "unknown" (never attempted).
        """
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        _create_ingest_run(
            "ec_donations",
            now - timedelta(hours=2),
            now - timedelta(hours=1),
            status="failed",
        )
        status = check_freshness_for_source("ec_donations", sla_days=7, now=now)
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
        # No IngestRun row exists at all for uk_contracts_finder in this test
        # (never attempted) -- "unknown", not "critical" (which now means
        # "attempted, but failed or too stale").
        assert results[1].label == "unknown"

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
        # source_c never got an IngestRun row in this test -- "unknown"
        # (never attempted), not "critical".
        assert labels["source_c"] == "unknown"


@pytest.mark.django_db
class TestGraphLayerCoverage:
    """The bug this pass fixes: no graph-layer connector (ec_donations,
    ch_officers, ch_appointments, lords_interests, parliament_interests,
    gleif) writes to IngestRun yet, so freshness checking must report that
    honestly instead of silently omitting or misreporting those sources.
    """

    def test_never_run_graph_source_is_reported_not_omitted(self):
        """GIVEN the real source register (sources/*.yml, default sources_dir)
        and a graph-layer source with zero IngestRun rows (true today for
        every graph connector)
        WHEN checking freshness across all registered sources
        THEN the graph source appears in the results (not omitted) labelled
        "unknown", proving a never-run connector is visible rather than
        invisible.
        """
        now = datetime(2026, 7, 23, 12, 0, tzinfo=UTC)
        results = check_freshness(now=now)  # default sources_dir=Path("sources")

        by_id = {r.source_id: r for r in results}
        assert "gleif" in by_id  # connector_kind: graph, never wired to IngestRun
        assert by_id["gleif"].label == "unknown"
        assert by_id["gleif"].days_since_success is None

    def test_recorded_run_yields_correct_freshness_label(self):
        """GIVEN a graph connector that records a COMPLETE run via
        run_recorder.record_ingest_run
        WHEN checking its freshness immediately afterwards
        THEN it is "fresh" -- proving the helper's write is what freshness.py
        reads, closing the gap between the two.
        """
        with record_ingest_run("gleif") as run:
            run.finish(Completeness.COMPLETE, records_fetched=500, records_ingested=500)

        status = check_freshness_for_source("gleif", sla_days=7)
        assert status.label == "fresh"
        assert status.is_within_sla is True

    def test_partial_run_is_not_reportable_as_healthy(self):
        """GIVEN a graph connector that records a PARTIAL run (fetched fewer
        records than expected) via run_recorder.record_ingest_run, made just
        now
        WHEN checking its freshness
        THEN the label is NOT "fresh" -- ADR-008: only COMPLETE may feed
        scoring, so a recent-but-partial run must not read as healthy.
        """
        with record_ingest_run("gleif") as run:
            run.finish(Completeness.PARTIAL, records_fetched=500, records_ingested=300)

        status = check_freshness_for_source("gleif", sla_days=7)
        assert status.label != "fresh"
        assert status.label == "critical"
