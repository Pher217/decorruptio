"""High share of contracts let by direct/non-competitive procedure.

Flags buyers where >30% of their tenders use direct/non-competitive
procedures. In Colombia, this maps to modalidad_de_contratacion containing
'directa'. In Ukraine, procurementMethodType containing 'limited' or
'negotiation' or 'reporting'.
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

# Direct award keywords by source
DIRECT_KEYWORDS = {
    "ua_prozorro": ["limited", "negotiation", "reporting", "direct"],
    "co_secop_ii": ["directa"],
    "uk_contracts_finder": ["direct", "single_tender"],
}


class DirectAwardShare(Indicator):
    id = "i005_direct_award_share"
    title = "Direct-award share"
    definition = "High share of contracts let by direct/non-competitive procedure."
    inputs: list[str] = ["tender.procurementMethod"]
    params: dict[str, Any] = {"share_threshold": 0.3}
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
            SELECT source_id, buyer_name,
                   COUNT(*) as total_tenders,
                   source_url, raw_json,
                   procurement_method, procurement_method_details
            FROM tenders
            WHERE buyer_name IS NOT NULL
            GROUP BY source_id, buyer_name, source_url, raw_json,
                     procurement_method, procurement_method_details
            """
        ).fetchall()

        # Aggregate per buyer
        buyers: dict[str, dict[str, Any]] = {}
        for row in rows:
            source_id, buyer, total, url, raw, method, method_details = row
            if buyer not in buyers:
                buyers[buyer] = {
                    "source_id": source_id,
                    "total": 0,
                    "direct": 0,
                    "url": url,
                    "raw": raw,
                }
            buyers[buyer]["total"] += total
            method_str = f"{method or ''} {method_details or ''}".lower()
            keywords = DIRECT_KEYWORDS.get(source_id, ["direct"])
            if any(kw in method_str for kw in keywords):
                buyers[buyer]["direct"] += total

        for buyer, data in buyers.items():
            if data["total"] < 3:
                continue
            share = data["direct"] / data["total"] if data["total"] > 0 else 0
            if share >= threshold:
                raw = data["raw"]
                yield Flag(
                    indicator_id=self.id,
                    subject_ref=buyer,
                    as_of=today,
                    explanation=(
                        f"Buyer '{buyer}' used direct/non-competitive procedures "
                        f"for {data['direct']} of {data['total']} tenders "
                        f"({share:.0%}). Threshold: {threshold:.0%}."
                    ),
                    evidence=[
                        ProvenanceRecord(
                            source_id=data["source_id"],
                            source_url=data["url"],
                            retrieved_at=datetime.now(UTC),
                            content_hash=hashlib.sha256(
                                raw.encode() if isinstance(raw, str) else raw
                            ).hexdigest(),
                            license="Open data",
                            redistribution=Redistribution.OPEN,
                            jurisdiction=ctx.locale.code.upper(),
                            data_class=DataClass.A1,
                            tier=Tier.A,
                            connector=data["source_id"],
                            connector_version="0.1",
                        )
                    ],
                    stamp=VersionStamp(
                        data_snapshot=today.isoformat(),
                        code_version="0.0.1",
                        indicator_version=self.id,
                    ),
                )
