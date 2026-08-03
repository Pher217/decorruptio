"""Ergonomic ingest-run recording — the second link in ADR-008's fail-closed
measurement boundary (response -> ingest run -> dataset snapshot -> case
classification -> verdict). Every ingest terminates in exactly one of the
five ADR-008 completeness states; only `COMPLETE` may feed scoring.

Why this module exists instead of connectors calling `IngestRun.objects.create`
directly: `staging.models.IngestRun.status` is a 3-value field
(running/success/failed, `max_length=10`) that predates ADR-008's five-value
lattice and is too narrow to store it as-is — `"unverifiable"` alone is 12
characters, which would raise on the project's real PostgreSQL backend
(`config/settings/base.py`) even though the sqlite test backend would let it
through silently. Widening the column and adding a dedicated
`records_fetched` field is a schema migration, and this pass is explicitly
not permitted to generate one (Django migrations are human-applied here).

So, until that migration lands, this module compresses losslessly-on-paper,
lossily-in-the-narrow-column:

- `IngestRun.status` is set to `"success"` only when completeness is
  `COMPLETE`, else `"failed"` — so `pipelines.freshness`'s existing
  `status="success"` query keeps working unmodified, and a `PARTIAL`,
  `BLOCKED`, `UNVERIFIABLE`, or `FAILED` run is never reportable as healthy.
- `IngestRun.rows_ingested` stores `records_ingested`.
- `IngestRun.error_message` carries the full ADR-008 completeness value plus
  `records_fetched`/`records_ingested`, encoded as one parseable line (see
  `encode_completeness_note` / `parse_completeness_note`) — so the
  fine-grained status is not silently discarded, only compressed for the one
  column that cannot yet hold it directly. This is exactly the failure mode
  ADR-008 names ("a status was known somewhere and discarded before it
  reached the decision") applied to the tool meant to prevent it, mitigated
  with the schema available today.

TODO (human-authored migration, out of this pass's scope): widen
`IngestRun.status` to `max_length=12` with `choices` extended to
`COMPLETE`/`PARTIAL`/`BLOCKED`/`FAILED`/`UNVERIFIABLE` (keep `running` as the
transient pre-terminal state), and add `records_fetched =
models.IntegerField(default=0)` alongside the existing `rows_ingested`. Once
that lands, this module's encoding collapses to a straight passthrough and
`parse_completeness_note` becomes unnecessary.

Usage (connector adoption is a follow-up — no connector calls this yet)::

    from uncorrupt.pipelines.run_recorder import Completeness, record_ingest_run

    with record_ingest_run("gleif") as run:
        fetch_result = fetch_gleif(...)
        ingest_result = ingest_gleif(fetch_result, ...)
        run.finish(
            Completeness.COMPLETE,
            records_fetched=fetch_result.record_count,
            records_ingested=ingest_result.new_count,
        )

If the block raises, the run is recorded `FAILED` (with the exception text)
and the exception re-raised — a run is never left stuck in `"running"`
because of an unhandled error. If the block exits without calling
`run.finish(...)`, the run is recorded `UNVERIFIABLE` rather than defaulting
to healthy — a forgotten call must fail closed, not fail open.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from uncorrupt.staging.models import IngestRun

_NOTE_PREFIX = "completeness="
_FETCHED_PREFIX = " fetched="
_INGESTED_PREFIX = " ingested="
_DETAIL_PREFIX = " detail="


class Completeness(StrEnum):
    """ADR-008's five-value completeness lattice. Only COMPLETE may feed scoring."""

    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    BLOCKED = "BLOCKED"
    FAILED = "FAILED"
    UNVERIFIABLE = "UNVERIFIABLE"


@dataclass(frozen=True)
class CompletenessNote:
    """The ADR-008 detail recovered from an `IngestRun.error_message` note."""

    completeness: Completeness
    records_fetched: int
    records_ingested: int
    detail: str | None


def encode_completeness_note(
    completeness: Completeness,
    *,
    records_fetched: int,
    records_ingested: int,
    detail: str | None = None,
) -> str:
    """Encode ADR-008 completeness detail into one `IngestRun.error_message` line."""
    note = (
        f"{_NOTE_PREFIX}{completeness}{_FETCHED_PREFIX}{records_fetched}"
        f"{_INGESTED_PREFIX}{records_ingested}"
    )
    if detail:
        note += f"{_DETAIL_PREFIX}{detail}"
    return note


def parse_completeness_note(error_message: str | None) -> CompletenessNote | None:
    """Recover the ADR-008 completeness detail `encode_completeness_note` wrote.

    Returns None if `error_message` wasn't produced by this module (e.g. a
    legacy row, or a run recorded some other way) — callers must not guess.
    """
    if not error_message or not error_message.startswith(_NOTE_PREFIX):
        return None

    detail: str | None = None
    body = error_message
    if _DETAIL_PREFIX in body:
        body, detail = body.split(_DETAIL_PREFIX, 1)

    try:
        completeness_part, rest = body[len(_NOTE_PREFIX) :].split(_FETCHED_PREFIX, 1)
        fetched_part, ingested_part = rest.split(_INGESTED_PREFIX, 1)
        return CompletenessNote(
            completeness=Completeness(completeness_part),
            records_fetched=int(fetched_part),
            records_ingested=int(ingested_part),
            detail=detail,
        )
    except (ValueError, KeyError):
        return None


@dataclass
class _RunRecorder:
    """The object yielded by `record_ingest_run` — call `.finish(...)` on it."""

    source_id: str
    _completeness: Completeness | None = None
    _records_fetched: int = 0
    _records_ingested: int = 0
    _detail: str | None = None

    def finish(
        self,
        completeness: Completeness,
        *,
        records_fetched: int,
        records_ingested: int,
        detail: str | None = None,
    ) -> None:
        """Declare the run's ADR-008 completeness. Call exactly once per run."""
        self._completeness = completeness
        self._records_fetched = records_fetched
        self._records_ingested = records_ingested
        self._detail = detail


@contextmanager
def record_ingest_run(
    source_id: str,
    *,
    now: datetime | None = None,
) -> Iterator[_RunRecorder]:
    """Record one `IngestRun` for `source_id`, ADR-008-complete.

    Yields a `_RunRecorder` — call `.finish(completeness, records_fetched=,
    records_ingested=)` on it before the block ends. An exception inside the
    block records the run `FAILED` and re-raises. A block that exits without
    calling `.finish(...)` records `UNVERIFIABLE` — fail closed, not open.
    """
    started_at = now if now is not None else datetime.now(UTC)
    recorder = _RunRecorder(source_id=source_id)
    try:
        yield recorder
    except Exception as exc:
        IngestRun.objects.create(
            source_id=source_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            status="failed",
            rows_ingested=0,
            error_message=encode_completeness_note(
                Completeness.FAILED,
                records_fetched=0,
                records_ingested=0,
                detail=str(exc),
            ),
        )
        raise
    else:
        completeness = recorder._completeness or Completeness.UNVERIFIABLE
        IngestRun.objects.create(
            source_id=source_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            status="success" if completeness == Completeness.COMPLETE else "failed",
            rows_ingested=recorder._records_ingested,
            error_message=encode_completeness_note(
                completeness,
                records_fetched=recorder._records_fetched,
                records_ingested=recorder._records_ingested,
                detail=recorder._detail,
            ),
        )
