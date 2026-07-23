"""Single-bidder award: only one valid bid received (World Bank / OCP i001).

Flags tenders where exactly one bid was received and an award was made.
In Colombia SECOP II, this maps to proveedores_que_manifestaron == 1.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date
from typing import Any

import duckdb

from uncorrupt.core.provenance import ProvenanceRecord, Redistribution, VersionStamp
from uncorrupt.core.tiers import DataClass, Tier
from uncorrupt.indicators.base import Flag, Indicator, ValidationStatus
from uncorrupt.indicators.context import EvaluationContext


class SingleBidder(Indicator):
    id = "i001_single_bidder"
    title = "Single bidder"
    definition = "Single-bidder award: only one valid bid received (World Bank / OCP i001)."
    inputs: list[str] = ["tenders", "bids", "awards"]
    params: dict[str, Any] = {}
    validation: dict[str, ValidationStatus] = {
        "ua": ValidationStatus.VALIDATED,
        "co": ValidationStatus.VALIDATED,
        "gb": ValidationStatus.VALIDATED,
    }

    def evaluate(self, records: Any, ctx: EvaluationContext) -> Iterator[Flag]:
        """records is a duckdb connection with staging tables populated."""
        conn: duckdb.DuckDBPyConnection = records
        today = date.today()

        # For sources with a bids table (ProZorro)
        rows = conn.execute(
            """
            SELECT t.source_id, t.tender_id, t.title, t.buyer_name,
                   t.source_url, t.raw_json,
                   COUNT(b.bid_id) as bid_count
            FROM tenders t
            JOIN awards a ON t.source_id = a.source_id AND t.tender_id = a.tender_id
            LEFT JOIN bids b ON t.source_id = b.source_id AND t.tender_id = b.tender_id
            WHERE t.source_id = 'ua_prozorro'
            GROUP BY t.source_id, t.tender_id, t.title, t.buyer_name, t.source_url, t.raw_json
            HAVING bid_count = 1
            """
        ).fetchall()

        for row in rows:
            source_id, tender_id, title, buyer, url, raw, bid_count = row
            raw_data = json.loads(raw) if isinstance(raw, str) else raw
            yield Flag(
                indicator_id=self.id,
                subject_ref=tender_id,
                as_of=today,
                explanation=(
                    f"Tender '{title or tender_id}' awarded with only {bid_count} bid. "
                    f"Buyer: {buyer}. Single-bidder awards are the strongest "
                    "predictor of corruption in procurement (World Bank)."
                ),
                evidence=[
                    ProvenanceRecord(
                        source_id=source_id,
                        source_url=url,
                        retrieved_at=__import__("datetime").datetime.now(UTC),
                        content_hash=hashlib_sha256(raw),
                        license="Open data",
                        redistribution=Redistribution.OPEN,
                        jurisdiction="UA",
                        data_class=DataClass.A1,
                        tier=Tier.A,
                        connector="ua_prozorro",
                        connector_version="0.1",
                    )
                ],
                stamp=VersionStamp(
                    data_snapshot=today.isoformat(),
                    code_version="0.0.1",
                    indicator_version=self.id,
                ),
            )

        # For Colombia SECOP II: check proveedores_que_manifestaron in raw_json
        co_rows = conn.execute(
            """
            SELECT t.source_id, t.tender_id, t.title, t.buyer_name,
                   t.source_url, t.raw_json
            FROM tenders t
            JOIN awards a ON t.source_id = a.source_id AND t.tender_id = a.tender_id
            WHERE t.source_id = 'co_secop_ii'
            """
        ).fetchall()

        for row in co_rows:
            source_id, tender_id, title, buyer, url, raw = row
            raw_data = json.loads(raw) if isinstance(raw, str) else raw
            manifest_count = _safe_int(raw_data.get("proveedores_que_manifestaron", 0))
            if manifest_count == 1:
                yield Flag(
                    indicator_id=self.id,
                    subject_ref=tender_id,
                    as_of=today,
                    explanation=(
                        f"Process '{title or tender_id}' awarded with only 1 supplier "
                        f"manifesting interest. Buyer: {buyer}. "
                        f"Modalidad: {raw_data.get('modalidad_de_contratacion', '?')}."
                    ),
                    evidence=[
                        ProvenanceRecord(
                            source_id=source_id,
                            source_url=url,
                            retrieved_at=__import__("datetime").datetime.now(UTC),
                            content_hash=hashlib_sha256(raw),
                            license="Open data",
                            redistribution=Redistribution.OPEN,
                            jurisdiction="CO",
                            data_class=DataClass.A1,
                            tier=Tier.A,
                            connector="co_secop_ii",
                            connector_version="0.1",
                        )
                    ],
                    stamp=VersionStamp(
                        data_snapshot=today.isoformat(),
                        code_version="0.0.1",
                        indicator_version=self.id,
                    ),
                )


def _safe_int(v: Any) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return 0


def hashlib_sha256(data: str | bytes) -> str:
    import hashlib

    if isinstance(data, str):
        data = data.encode()
    return hashlib.sha256(data).hexdigest()
