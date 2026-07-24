"""Bid submission window shorter than the legal/typical minimum (OCP).

Flags tenders where the bid submission period (tender_start → tender_end)
is shorter than the locale's legal minimum.
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

    def evaluate(self, ctx: EvaluationContext) -> Iterator[Flag]:
        today = date.today()
        min_days = ctx.locale.procedure_metadata.get("min_bid_days_open", self.params["min_days"])

        tenders = Tender.objects.filter(
            source_id=ctx.source_id,
            tender_start__isnull=False,
            tender_end__isnull=False,
        )
        for t in tenders:
            assert t.tender_start is not None and t.tender_end is not None
            window_days = (t.tender_end - t.tender_start).days
            if window_days < min_days:
                yield Flag(
                    indicator_id=self.id,
                    subject_ref=t.tender_id,
                    as_of=today,
                    explanation=(
                        f"Tender '{t.title or t.tender_id}' had a bid window of only "
                        f"{window_days} days (minimum: {min_days}). "
                        f"Buyer: {t.buyer_name}. Short windows suppress competition."
                    ),
                    evidence=[
                        ProvenanceRecord(
                            source_id=t.source_id,
                            source_url=t.source_url,
                            retrieved_at=datetime.now(UTC),
                            content_hash=hashlib.sha256(
                                json.dumps(t.raw_json).encode()
                            ).hexdigest(),
                            license="Open data",
                            redistribution=Redistribution.OPEN,
                            jurisdiction=ctx.locale.code.upper(),
                            data_class=DataClass.A1,
                            tier=Tier.A,
                            connector=t.source_id,
                            connector_version="0.1",
                        )
                    ],
                    stamp=VersionStamp(
                        data_snapshot=today.isoformat(),
                        code_version="0.0.1",
                        indicator_version=self.id,
                    ),
                )
