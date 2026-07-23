"""Single-bidder award: only one valid bid received (World Bank / OCP i001).

Flags tenders where exactly one bid was received and an award was made.
In Colombia SECOP II, this maps to proveedores_que_manifestaron == 1.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

from django.db.models import Count

from uncorrupt.core.provenance import ProvenanceRecord, Redistribution, VersionStamp
from uncorrupt.core.tiers import DataClass, Tier
from uncorrupt.indicators.base import Flag, Indicator, ValidationStatus
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.staging.models import Tender


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

    def evaluate(self, ctx: EvaluationContext) -> Iterator[Flag]:
        today = date.today()

        # Ukraine ProZorro: tenders with awards and exactly 1 bid
        ua_tenders = (
            Tender.objects.filter(source_id="ua_prozorro", awards__isnull=False)
            .annotate(bid_count=Count("bids"))
            .filter(bid_count=1)
        )
        for t in ua_tenders:
            yield Flag(
                indicator_id=self.id,
                subject_ref=t.tender_id,
                as_of=today,
                explanation=(
                    f"Tender '{t.title or t.tender_id}' awarded with only 1 bid. "
                    f"Buyer: {t.buyer_name}. Single-bidder awards are the strongest "
                    "predictor of corruption in procurement (World Bank)."
                ),
                evidence=[
                    ProvenanceRecord(
                        source_id=t.source_id,
                        source_url=t.source_url,
                        retrieved_at=datetime.now(UTC),
                        content_hash=hashlib.sha256(json.dumps(t.raw_json).encode()).hexdigest(),
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

        # Colombia SECOP II: check proveedores_que_manifestaron in raw_json
        co_tenders = Tender.objects.filter(source_id="co_secop_ii", awards__isnull=False)
        for t in co_tenders:  # type: ignore[assignment]
            manifest_count = _safe_int(t.raw_json.get("proveedores_que_manifestaron", 0))
            if manifest_count == 1:
                yield Flag(
                    indicator_id=self.id,
                    subject_ref=t.tender_id,
                    as_of=today,
                    explanation=(
                        f"Process '{t.title or t.tender_id}' awarded with only 1 supplier "
                        f"manifesting interest. Buyer: {t.buyer_name}. "
                        f"Modalidad: {t.raw_json.get('modalidad_de_contratacion', '?')}."
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

        # UK Contracts Finder: no bids table — flag if only 1 award
        uk_tenders = (
            Tender.objects.filter(source_id="uk_contracts_finder")
            .annotate(award_count=Count("awards"))
            .filter(award_count=1)
        )
        for t in uk_tenders:  # type: ignore[assignment]
            yield Flag(
                indicator_id=self.id,
                subject_ref=t.tender_id,
                as_of=today,
                explanation=(
                    f"Tender '{t.title or t.tender_id}' awarded with only 1 award. "
                    f"Buyer: {t.buyer_name}. Single-award contracts may indicate "
                    "lack of competition (UK Contracts Finder has no bid-level data)."
                ),
                evidence=[
                    ProvenanceRecord(
                        source_id=t.source_id,
                        source_url=t.source_url,
                        retrieved_at=datetime.now(UTC),
                        content_hash=hashlib.sha256(json.dumps(t.raw_json).encode()).hexdigest(),
                        license="Open data",
                        redistribution=Redistribution.OPEN,
                        jurisdiction="GB",
                        data_class=DataClass.A1,
                        tier=Tier.A,
                        connector="uk_contracts_finder",
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
