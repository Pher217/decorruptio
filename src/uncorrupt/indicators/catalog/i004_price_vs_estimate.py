"""Award value deviates sharply from the tender estimate (over/under-pricing).

Flags awards where the award value differs from the tender value by >25%.
In Colombia SECOP II, maps to precio_base vs valor_total_adjudicacion.
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


class PriceVsEstimate(Indicator):
    id = "i004_price_vs_estimate"
    title = "Price vs estimate"
    definition = "Award value deviates sharply from the tender estimate (over/under-pricing)."
    inputs: list[str] = ["tender.value", "awards.value"]
    params: dict[str, Any] = {"pct_threshold": 0.25}
    validation: dict[str, ValidationStatus] = {
        "ua": ValidationStatus.VALIDATED,
        "co": ValidationStatus.VALIDATED,
        "gb": ValidationStatus.VALIDATED,
    }

    def evaluate(self, records: Any, ctx: EvaluationContext) -> Iterator[Flag]:
        conn: duckdb.DuckDBPyConnection = records
        today = date.today()
        threshold = self.params["pct_threshold"]

        rows = conn.execute(
            """
            SELECT a.source_id, a.tender_id, a.award_id,
                   a.supplier_name, a.value_amount as award_value,
                   t.value_amount as tender_value, t.title, t.source_url,
                   t.raw_json
            FROM awards a
            JOIN tenders t ON a.source_id = t.source_id AND a.tender_id = t.tender_id
            WHERE a.value_amount IS NOT NULL
              AND t.value_amount IS NOT NULL
              AND t.value_amount > 0
              AND ABS(a.value_amount - t.value_amount) / t.value_amount >= ?
            """,
            [threshold],
        ).fetchall()

        for row in rows:
            (
                source_id,
                tender_id,
                award_id,
                supplier,
                award_value,
                tender_value,
                title,
                source_url,
                raw,
            ) = row
            deviation = abs(award_value - tender_value) / tender_value
            direction = "below" if award_value < tender_value else "above"
            yield Flag(
                indicator_id=self.id,
                subject_ref=f"{tender_id}:{award_id}",
                as_of=today,
                explanation=(
                    f"Award to '{supplier}' was {deviation:.0%} {direction} the "
                    f"tender estimate (tender: {tender_value:,.0f}, "
                    f"award: {award_value:,.0f}). "
                    f"Tender: '{title or tender_id}'."
                ),
                evidence=[
                    ProvenanceRecord(
                        source_id=source_id,
                        source_url=source_url,
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
