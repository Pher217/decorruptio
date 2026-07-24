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
        source = ctx.source_id

        if source == "ua_prozorro":
            # Tenders with awards and exactly 1 bid
            ua_tenders: Any = (
                Tender.objects.filter(source_id=source, awards__isnull=False)
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
                            content_hash=hashlib.sha256(
                                json.dumps(t.raw_json).encode()
                            ).hexdigest(),
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

        elif source == "co_secop_ii":
            # Check unique suppliers with offers in raw_json
            co_tenders = Tender.objects.filter(source_id=source, awards__isnull=False)
            for t in co_tenders:
                # proveedores_que_manifestaron is often 0; proveedores_unicos_con
                # (unique suppliers with responses) is the meaningful count
                manifest_count = _safe_int(t.raw_json.get("proveedores_que_manifestaron", 0))
                unique_count = _safe_int(t.raw_json.get("proveedores_unicos_con", 0))
                supplier_count = max(manifest_count, unique_count)
                if supplier_count == 1:
                    yield Flag(
                        indicator_id=self.id,
                        subject_ref=t.tender_id,
                        as_of=today,
                        explanation=(
                            f"Process '{t.title or t.tender_id}' awarded with only 1 "
                            f"supplier manifesting interest. Buyer: {t.buyer_name}. "
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

        elif source == "uk_contracts_finder":
            # No bid-level data — flag only genuinely competitive procedures where
            # a single award suggests suppressed competition.
            #
            # UK Contracts Finder procedure types:
            # - "Call-off from a framework agreement" → single-supplier BY DESIGN,
            #   NOT suspicious — EXCLUDE
            # - "Competitive procedure with negotiation" → competitive, single
            #   award IS suspicious — INCLUDE
            # - "Open", "Restricted" → competitive by definition — INCLUDE
            # - "Negotiated without prior publication" → direct award, i005 covers
            #   those — EXCLUDE from i001
            #
            # The signal is only real when the procedure was meant to be competitive.
            NON_COMPETITIVE_KEYWORDS = [
                "call-off",
                "call off",
                "framework",
                "negotiated without prior publication",
                "direct",
            ]
            COMPETITIVE_KEYWORDS = [
                "open",
                "competitive",
                "restricted",
            ]
            uk_tenders: Any = (
                Tender.objects.filter(source_id=source)
                .annotate(award_count=Count("awards"))
                .filter(award_count=1)
            )
            for t in uk_tenders:
                method_str = (
                    f"{t.procurement_method or ''} {t.procurement_method_details or ''}"
                ).lower()
                # Exclude non-competitive procedures (framework call-offs, direct)
                if any(kw in method_str for kw in NON_COMPETITIVE_KEYWORDS):
                    continue
                # Only flag if the procedure is genuinely competitive
                if not any(kw in method_str for kw in COMPETITIVE_KEYWORDS):
                    continue
                yield Flag(
                    indicator_id=self.id,
                    subject_ref=t.tender_id,
                    as_of=today,
                    explanation=(
                        f"Tender '{t.title or t.tender_id}' used a competitive procedure "
                        f"({t.procurement_method or t.procurement_method_details or 'open'}) "
                        f"but resulted in a single award. "
                        f"Buyer: {t.buyer_name}. Single award in a competitive process "
                        "may indicate rigged specs or suppressed competition."
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
