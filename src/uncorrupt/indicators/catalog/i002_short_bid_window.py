"""Bid submission window shorter than the legal/typical minimum (OCP).

Flags tenders where the bid submission period (tender_start → tender_end)
is shorter than the locale's legal minimum.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import duckdb

from uncorrupt.core.provenance import ProvenanceRecord, Redistribution, VersionStamp
from uncorrupt.core.tiers import DataClass, Tier
from uncorrupt.indicators.base import Flag, Indicator, ValidationStatus
from uncorrupt.indicators.context import EvaluationContext


class ShortBidWindow(Indicator):
    id = "i002_short_bid_window"
    title = "Short bid window"
    definition = "Bid submission window shorter than the legal/typical minimum (OCP)."
    inputs: list[str] = ["tender.tenderPeriod"]
    params: dict[str, Any] = {"min_days": 15}
    validation: dict[str, ValidationStatus] = {
        "ua": ValidationStatus.VALIDATED,
        "co": ValidationStatus.VALIDATED,
        "gb": ValidationStatus.VALIDATED,
    }

    def evaluate(self, records: Any, ctx: EvaluationContext) -> Iterator[Flag]:
        conn: duckdb.DuckDBPyConnection = records
        today = date.today()
        min_days = ctx.locale.procedure_metadata.get("min_bid_days_open", self.params["min_days"])

        rows = conn.execute(
            """
            SELECT source_id, tender_id, title, buyer_name,
                   source_url, raw_json,
                   tender_start, tender_end,
                   date_diff('day', CAST(tender_start AS DATE),
                             CAST(tender_end AS DATE)) as window_days
            FROM tenders
            WHERE tender_start IS NOT NULL AND tender_end IS NOT NULL
              AND date_diff('day', CAST(tender_start AS DATE),
                             CAST(tender_end AS DATE)) < ?
            """,
            [min_days],
        ).fetchall()

        for row in rows:
            source_id, tender_id, title, buyer, url, raw, start, end, window_days = row
            yield Flag(
                indicator_id=self.id,
                subject_ref=tender_id,
                as_of=today,
                explanation=(
                    f"Tender '{title or tender_id}' had a bid window of only "
                    f"{window_days} days (minimum: {min_days}). "
                    f"Buyer: {buyer}. Short windows suppress competition."
                ),
                evidence=[
                    ProvenanceRecord(
                        source_id=source_id,
                        source_url=url,
                        retrieved_at=datetime.now(UTC),
                        content_hash=hashlib.sha256(
                            raw.encode() if isinstance(raw, str) else raw
                        ).hexdigest(),
                        license="Open data",
                        redistribution=Redistribution.OPEN,
                        jurisdiction=ctx.locale.code.upper(),
                        data_class=DataClass.A1,
                        tier=Tier.A,
                        connector=source_id,
                        connector_version="0.1",
                    )
                ],
                stamp=VersionStamp(
                    data_snapshot=today.isoformat(),
                    code_version="0.0.1",
                    indicator_version=self.id,
                ),
            )
