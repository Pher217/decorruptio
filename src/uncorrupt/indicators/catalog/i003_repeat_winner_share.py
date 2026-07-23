"""One supplier wins an outsized share of a buyer's awards over a window.

Flags buyer-supplier pairs where one supplier wins >50% of a buyer's
awards (by count) over the full dataset.
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


class RepeatWinnerShare(Indicator):
    id = "i003_repeat_winner_share"
    title = "Repeat-winner share"
    definition = "One supplier wins an outsized share of a buyer's awards over a window."
    inputs: list[str] = ["awards", "parties"]
    params: dict[str, Any] = {"share_threshold": 0.5}
    validation: dict[str, ValidationStatus] = {
        "ua": ValidationStatus.VALIDATED,
        "co": ValidationStatus.VALIDATED,
        "gb": ValidationStatus.VALIDATED,
    }

    def evaluate(self, records: Any, ctx: EvaluationContext) -> Iterator[Flag]:
        conn: duckdb.DuckDBPyConnection = records
        today = date.today()
        threshold = self.params["share_threshold"]

        rows = conn.execute(
            """
            WITH buyer_supplier AS (
                SELECT t.source_id, t.buyer_name, a.supplier_name,
                       COUNT(*) as award_count,
                       SUM(a.value_amount) as total_value,
                       MAX(t.source_url) as source_url
                FROM awards a
                JOIN tenders t ON a.source_id = t.source_id AND a.tender_id = t.tender_id
                WHERE a.supplier_name IS NOT NULL AND t.buyer_name IS NOT NULL
                GROUP BY t.source_id, t.buyer_name, a.supplier_name
            ),
            buyer_totals AS (
                SELECT source_id, buyer_name, COUNT(*) as total_awards
                FROM buyer_supplier
                GROUP BY source_id, buyer_name
            )
            SELECT bs.*, bt.total_awards,
                   CAST(bs.award_count AS DOUBLE) / bt.total_awards as share
            FROM buyer_supplier bs
            JOIN buyer_totals bt ON bs.source_id = bt.source_id AND bs.buyer_name = bt.buyer_name
            WHERE CAST(bs.award_count AS DOUBLE) / bt.total_awards >= ?
              AND bt.total_awards >= 2
            """,
            [threshold],
        ).fetchall()

        for row in rows:
            (
                source_id,
                buyer,
                supplier,
                award_count,
                total_value,
                source_url,
                total_awards,
                share,
            ) = row
            yield Flag(
                indicator_id=self.id,
                subject_ref=f"{buyer}→{supplier}",
                as_of=today,
                explanation=(
                    f"Supplier '{supplier}' won {award_count} of {total_awards} "
                    f"awards ({share:.0%}) from buyer '{buyer}'. "
                    f"Repeat-winner concentration above {threshold:.0%} threshold."
                ),
                evidence=[
                    ProvenanceRecord(
                        source_id=source_id,
                        source_url=source_url,
                        retrieved_at=datetime.now(UTC),
                        content_hash=hashlib.sha256(
                            f"{source_id}:{buyer}:{supplier}".encode()
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
