"""Award value deviates sharply from the tender estimate (over/under-pricing).

Flags awards where the award value differs from the tender value by >25%.
In Colombia SECOP II, maps to precio_base vs valor_total_adjudicacion.
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
from uncorrupt.staging.models import Award


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

    def evaluate(self, ctx: EvaluationContext) -> Iterator[Flag]:
        today = date.today()
        threshold = self.params["pct_threshold"]

        awards = Award.objects.select_related("tender_ref").filter(
            value_amount_cents__gt=0,
            tender_ref__value_amount_cents__gt=0,
        )
        for a in awards:
            if a.tender_ref is None:
                continue
            tender_value = a.tender_ref.value_amount_cents
            award_value = a.value_amount_cents
            deviation = abs(award_value - tender_value) * 100 / tender_value
            if deviation / 100 >= threshold:
                direction = "below" if award_value < tender_value else "above"
                yield Flag(
                    indicator_id=self.id,
                    subject_ref=f"{a.tender_id}:{a.award_id}",
                    as_of=today,
                    explanation=(
                        f"Award to '{a.supplier_name}' was {deviation / 100:.0%} "
                        f"{direction} the tender estimate "
                        f"(tender: {tender_value / 100:,.2f}, "
                        f"award: {award_value / 100:,.2f}). "
                        f"Tender: '{a.tender_ref.title or a.tender_id}'."
                    ),
                    evidence=[
                        ProvenanceRecord(
                            source_id=a.source_id,
                            source_url=a.tender_ref.source_url,
                            retrieved_at=datetime.now(UTC),
                            content_hash=hashlib.sha256(
                                json.dumps(a.tender_ref.raw_json).encode()
                            ).hexdigest(),
                            license="Open data",
                            redistribution=Redistribution.OPEN,
                            jurisdiction=ctx.locale.code.upper(),
                            data_class=DataClass.A1,
                            tier=Tier.A,
                            connector=a.source_id,
                            connector_version="0.1",
                        )
                    ],
                    stamp=VersionStamp(
                        data_snapshot=today.isoformat(),
                        code_version="0.0.1",
                        indicator_version=self.id,
                    ),
                )
