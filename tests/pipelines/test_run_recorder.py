"""Tests for run_recorder.py -- the ADR-008 ingest-run recording helper."""

import pytest

from uncorrupt.pipelines.run_recorder import (
    Completeness,
    encode_completeness_note,
    parse_completeness_note,
    record_ingest_run,
)
from uncorrupt.staging.models import IngestRun


@pytest.mark.django_db
class TestRecordIngestRun:
    """The context manager connectors call to record a run."""

    def test_complete_run_is_recorded_as_success(self):
        """GIVEN a connector run that finishes COMPLETE
        WHEN the record_ingest_run block exits normally
        THEN one IngestRun row is created with status "success" and
        rows_ingested set from records_ingested.
        """
        with record_ingest_run("gleif") as run:
            run.finish(Completeness.COMPLETE, records_fetched=100, records_ingested=100)

        rows = IngestRun.objects.filter(source_id="gleif")
        assert rows.count() == 1
        assert rows.first().status == "success"
        assert rows.first().rows_ingested == 100

    def test_partial_run_is_recorded_as_failed_in_the_coarse_column(self):
        """GIVEN a connector run that finishes PARTIAL
        WHEN the block exits normally
        THEN the coarse IngestRun.status is "failed" (not "success") -- so
        freshness.py's existing status="success" query never counts a
        partial run as healthy -- but rows_ingested reflects what was
        actually ingested, and the completeness note preserves PARTIAL.
        """
        with record_ingest_run("gleif") as run:
            run.finish(Completeness.PARTIAL, records_fetched=500, records_ingested=300)

        row = IngestRun.objects.get(source_id="gleif")
        assert row.status == "failed"
        assert row.rows_ingested == 300
        note = parse_completeness_note(row.error_message)
        assert note.completeness == Completeness.PARTIAL
        assert note.records_fetched == 500
        assert note.records_ingested == 300

    def test_unfinished_run_defaults_to_unverifiable(self):
        """GIVEN a block that exits without calling run.finish(...)
        WHEN the run is recorded
        THEN completeness defaults to UNVERIFIABLE (fail closed), not
        COMPLETE (fail open).
        """
        with record_ingest_run("gleif"):
            pass  # forgot to call .finish(...)

        row = IngestRun.objects.get(source_id="gleif")
        assert row.status == "failed"
        note = parse_completeness_note(row.error_message)
        assert note.completeness == Completeness.UNVERIFIABLE

    def test_exception_inside_block_is_recorded_failed_and_reraised(self):
        """GIVEN a block that raises an exception
        WHEN the exception propagates out of record_ingest_run
        THEN one IngestRun row is recorded FAILED (never left "running")
        and the original exception is re-raised, not swallowed.
        """
        with pytest.raises(ValueError, match="boom"), record_ingest_run("gleif"):
            raise ValueError("boom")

        row = IngestRun.objects.get(source_id="gleif")
        assert row.status == "failed"
        note = parse_completeness_note(row.error_message)
        assert note.completeness == Completeness.FAILED
        assert note.detail == "boom"

    def test_finished_at_is_set(self):
        """GIVEN a completed run
        WHEN checking the recorded IngestRun row
        THEN finished_at is populated (freshness.py requires it to compute
        days-since-success).
        """
        with record_ingest_run("gleif") as run:
            run.finish(Completeness.COMPLETE, records_fetched=1, records_ingested=1)

        row = IngestRun.objects.get(source_id="gleif")
        assert row.finished_at is not None


class TestCompletenessNoteRoundTrip:
    """encode_completeness_note / parse_completeness_note are inverses."""

    def test_round_trip_without_detail(self):
        """GIVEN a completeness note encoded with no detail
        WHEN parsing it back
        THEN every field round-trips and detail is None.
        """
        note = encode_completeness_note(
            Completeness.PARTIAL, records_fetched=850, records_ingested=800
        )
        parsed = parse_completeness_note(note)
        assert parsed.completeness == Completeness.PARTIAL
        assert parsed.records_fetched == 850
        assert parsed.records_ingested == 800
        assert parsed.detail is None

    def test_round_trip_with_detail(self):
        """GIVEN a completeness note encoded with a detail string
        WHEN parsing it back
        THEN the detail round-trips exactly.
        """
        note = encode_completeness_note(
            Completeness.BLOCKED,
            records_fetched=0,
            records_ingested=0,
            detail="HTTP 429 rate limited",
        )
        parsed = parse_completeness_note(note)
        assert parsed.completeness == Completeness.BLOCKED
        assert parsed.detail == "HTTP 429 rate limited"

    def test_unrecognised_error_message_returns_none(self):
        """GIVEN an error_message that wasn't produced by encode_completeness_note
        WHEN parsing it
        THEN None is returned rather than a guessed value.
        """
        assert parse_completeness_note("some other legacy error string") is None

    def test_none_error_message_returns_none(self):
        """GIVEN error_message=None (e.g. a legacy successful run)
        WHEN parsing it
        THEN None is returned.
        """
        assert parse_completeness_note(None) is None
