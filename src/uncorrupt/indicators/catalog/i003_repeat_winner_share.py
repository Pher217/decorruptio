"""One supplier wins an outsized share of a buyer's awards over a window.

Flags buyer-supplier pairs where one supplier wins >50% of a buyer's
awards (by count) over the full dataset.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

from django.db.models import Count, Sum

from uncorrupt.core.provenance import ProvenanceRecord, Redistribution, VersionStamp
from uncorrupt.core.tiers import DataClass, Tier
from uncorrupt.indicators.base import Flag, Indicator, ValidationStatus
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.staging.models import Award


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

    def evaluate(self, ctx: EvaluationContext) -> Iterator[Flag]:
        today = date.today()
        threshold = self.params["share_threshold"]

        pairs = (
            Award.objects.filter(
                source_id=ctx.source_id,
                supplier_name__isnull=False,
                tender_ref__buyer_name__isnull=False,
            )
            .values("source_id", "tender_ref__buyer_name", "supplier_name")
            .annotate(
                award_count=Count("id"),
                total_value=Sum("value_amount_cents"),
            )
        )

        # Build buyer totals (distinct supplier-pairs per buyer = total awards).
        # Exclude placeholder/junk supplier names — they are not real entities
        # and produce meaningless concentration flags (e.g. "Various" won 4 of 5).
        buyer_totals: dict[tuple[str, str], int] = {}
        rows: list[dict[str, Any]] = []
        for row in pairs:
            supplier = row["supplier_name"] or ""
            if _is_placeholder_supplier(supplier):
                continue
            buyer = row["tender_ref__buyer_name"] or ""
            key = (row["source_id"], buyer)
            buyer_totals[key] = buyer_totals.get(key, 0) + row["award_count"]
            rows.append(dict(row))

        # Denominator: buyer-supplier pairs evaluated (after placeholder exclusion)
        self.units_evaluated = len(rows)

        # Only flag buyer-supplier pairs where the buyer has >= 4 total awards.
        # With fewer data points (e.g., 1-of-2 = 50%), the share is the statistical
        # floor, not a signal. Require >= 4 for the concentration to be meaningful.
        MIN_BUYER_AWARDS = 4

        for r in rows:
            source_id = r["source_id"]
            buyer = r["tender_ref__buyer_name"] or ""
            supplier = r["supplier_name"]
            award_count = r["award_count"]
            total_awards = buyer_totals[(source_id, buyer)]
            if total_awards < MIN_BUYER_AWARDS:
                continue
            share = award_count / total_awards
            if share >= threshold:
                source_url = ""
                tender = (
                    Award.objects.filter(
                        source_id=source_id,
                        supplier_name=supplier,
                        tender_ref__buyer_name=buyer,
                    )
                    .select_related("tender_ref")
                    .first()
                )
                if tender and tender.tender_ref:
                    source_url = tender.tender_ref.source_url
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


# Placeholder/junk supplier names that should never produce a concentration flag.
# Case-insensitive, trimmed. These are not real entities — grouping on them is meaningless.
_PLACEHOLDER_SUPPLIERS = frozenset(
    {
        "various",
        "multiple",
        "multiple suppliers",
        "n/a",
        "na",
        "none",
        "not applicable",
        "tbc",
        "unknown",
        "-",
        "",
    }
)


def _is_placeholder_supplier(name: str) -> bool:
    return name.strip().lower() in _PLACEHOLDER_SUPPLIERS
