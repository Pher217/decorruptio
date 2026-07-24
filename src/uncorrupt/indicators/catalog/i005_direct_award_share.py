"""High share of contracts let by direct/non-competitive procedure.

Flags buyers where >30% of their tenders use direct/non-competitive
procedures. In Colombia, this maps to modalidad_de_contratacion containing
'directa'. In Ukraine, procurementMethodType containing 'limited' or
'negotiation' or 'reporting'.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

from uncorrupt.core.provenance import ProvenanceRecord, Redistribution, VersionStamp
from uncorrupt.core.tiers import DataClass, Tier
from uncorrupt.indicators.base import Flag, Indicator, ValidationStatus
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.staging.models import Tender

# Direct award keywords by source
DIRECT_KEYWORDS = {
    "ua_prozorro": ["limited", "negotiation", "reporting", "direct"],
    "co_secop_ii": ["directa"],
    "uk_contracts_finder": ["direct", "single_tender"],
}

# Minimum tenders per buyer for the share to be statistically meaningful.
# With <4 tenders, a high share is the floor, not a signal (same logic as i003).
MIN_BUYER_AWARDS = 4


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

    def evaluate(self, ctx: EvaluationContext) -> Iterator[Flag]:
        today = date.today()
        threshold = self.params["share_threshold"]

        # Aggregate per buyer: total tenders and direct-award count
        buyers: dict[str, dict[str, Any]] = {}
        buyer_tenders = list(
            Tender.objects.filter(
                source_id=ctx.source_id,
                buyer_name__isnull=False,
            ).values(
                "source_id",
                "buyer_name",
                "procurement_method",
                "procurement_method_details",
                "source_url",
                "raw_json",
            )
        )
        # Group tender details per buyer for keyword checks + source_url
        for t in buyer_tenders:
            buyer = t["buyer_name"]
            if buyer is None:
                continue
            if buyer not in buyers:
                buyers[buyer] = {
                    "source_id": t["source_id"],
                    "total": 0,
                    "direct": 0,
                    "url": t["source_url"],
                    "raw": t["raw_json"],
                }
            buyers[buyer]["total"] += 1
            method_str = (
                f"{t['procurement_method'] or ''} {t['procurement_method_details'] or ''}"
            ).lower()
            keywords = DIRECT_KEYWORDS.get(t["source_id"], ["direct"])
            if any(kw in method_str for kw in keywords):
                buyers[buyer]["direct"] += 1

        # Denominator: distinct buyers evaluated
        self.units_evaluated = len(buyers)

        for buyer, data in buyers.items():
            # Minimum denominator guard: with <4 tenders, the share is the statistical
            # floor, not a signal (mirrors i003's MIN_BUYER_AWARDS).
            if data["total"] < MIN_BUYER_AWARDS:
                continue
            share = data["direct"] / data["total"] if data["total"] > 0 else 0
            if share >= threshold:
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
                                json.dumps(data["raw"]).encode()
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
