"""Award value deviates sharply from the tender estimate (over-pricing).

Flags awards where the award value is ABOVE the tender value by >25%.
Below-estimate awards are excluded from the default curated output because
in reverse-auction systems (Ukraine ProZorro) and ceiling-based systems
(Colombia precio_base), below-estimate is the system working as designed.

Below-estimate flags are emitted with a ``weak=True`` marker in the
explanation so they can be filtered out by the curation script.
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
from uncorrupt.indicators.catalog._framework import is_framework_or_dps
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.staging.models import Award


class PriceVsEstimate(Indicator):
    id = "i004_price_vs_estimate"
    title = "Price vs estimate"
    definition = "Award value deviates sharply from the tender estimate (over-pricing)."
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

        awards = (
            Award.objects.select_related("tender_ref")
            .filter(
                source_id=ctx.source_id,
                value_amount_cents__gt=0,
                tender_ref__value_amount_cents__gt=0,
            )
            .exclude(value_amount_cents__isnull=True)
        )
        self.units_evaluated = awards.count()
        for a in awards:
            if a.tender_ref is None:
                continue
            tender = a.tender_ref
            # Exclude framework/DPS tenders — the "estimate" is a ceiling, not a
            # market-price estimate. Price deviation there is meaningless.
            if is_framework_or_dps(
                tender.title,
                tender.procurement_method,
                tender.procurement_method_details,
                tender.raw_json,
            ):
                continue
            tender_value = tender.value_amount_cents or 0
            award_value = a.value_amount_cents or 0
            if tender_value == 0:
                continue
            deviation = abs(award_value - tender_value) * 100 / tender_value
            if deviation / 100 < threshold:
                continue

            is_above = award_value > tender_value
            direction = "above" if is_above else "below"
            weak_marker = "" if is_above else " [WEAK: below-estimate]"
            yield Flag(
                indicator_id=self.id,
                subject_ref=f"{a.tender_id}:{a.award_id}",
                as_of=today,
                explanation=(
                    f"Award to '{a.supplier_name}' was {deviation / 100:.0%} "
                    f"{direction} the tender estimate "
                    f"(tender: {tender_value / 100:,.2f}, "
                    f"award: {award_value / 100:,.2f}). "
                    f"Tender: '{tender.title or a.tender_id}'.{weak_marker}"
                ),
                evidence=[
                    ProvenanceRecord(
                        source_id=a.source_id,
                        source_url=tender.source_url,
                        retrieved_at=datetime.now(UTC),
                        content_hash=hashlib.sha256(
                            json.dumps(tender.raw_json).encode()
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
