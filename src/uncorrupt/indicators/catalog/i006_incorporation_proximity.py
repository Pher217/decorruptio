"""i006 incorporation_proximity — supplier incorporated < N days before award.

PPE Medpro case: incorporated 2020-05-12, won GBP 81m on 2020-06-12 (31 days later).
A company incorporated shortly before winning a large public contract is a red flag
for shell entities created to capture specific procurement opportunities.
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
from uncorrupt.staging.models import Award, SupplierResolution

# Default: supplier incorporated less than 90 days before the award date.
# 90 days is the threshold — a company incorporated within 3 months of winning
# a public contract is suspiciously new, especially for large awards.
INCORPORATION_PROXIMITY_DAYS = 90


class IncorporationProximity(Indicator):
    id = "i006_incorporation_proximity"
    title = "Incorporation proximity"
    definition = (
        "Supplier incorporated less than N days before the award date. "
        "Shell entities are often created shortly before winning specific contracts."
    )
    inputs: list[str] = ["awards", "companies"]
    params: dict[str, Any] = {"threshold_days": INCORPORATION_PROXIMITY_DAYS}
    validation: dict[str, ValidationStatus] = {
        "gb": ValidationStatus.VALIDATED,
        "ua": ValidationStatus.UNVALIDATED,
        "co": ValidationStatus.UNVALIDATED,
    }

    def evaluate(self, ctx: EvaluationContext) -> Iterator[Flag]:
        today = date.today()
        source = ctx.source_id

        awards = Award.objects.filter(source_id=source, status="active").select_related(
            "tender_ref"
        )

        # Build resolution lookup: supplier_name → (company_number, confidence, method)
        resolutions: dict[str, dict[str, Any]] = {}
        for res in SupplierResolution.objects.filter(source_id=source).exclude(
            company_number__isnull=True
        ):
            resolutions[res.supplier_name] = {
                "company_number": res.company_number,
                "confidence": res.match_confidence,
                "method": res.match_method,
            }

        # Evaluate only awards with a resolved supplier
        evaluable = [a for a in awards if a.supplier_name and a.supplier_name in resolutions]
        self.units_evaluated = len(evaluable)

        for award in evaluable:
            res = resolutions[award.supplier_name]
            company = _get_company(res["company_number"])
            if not company or not company.incorporation_date or not award.award_date:
                continue

            age_days = (award.award_date.date() - company.incorporation_date).days

            if 0 <= age_days < INCORPORATION_PROXIMITY_DAYS:
                confidence_note = _confidence_note(res)
                yield Flag(
                    indicator_id=self.id,
                    subject_ref=f"{award.tender_id}:{award.award_id}",
                    as_of=today,
                    explanation=(
                        f"Supplier '{award.supplier_name}' was incorporated only {age_days} days "
                        f"before winning award of {_fmt_value(award)} on "
                        f"{award.award_date.date().isoformat()}. "
                        f"Company number {company.company_number}, "
                        f"incorporated {company.incorporation_date.isoformat()}. "
                        f"A company this new winning a public contract may be a shell entity "
                        f"created to capture this procurement.{confidence_note}"
                    ),
                    evidence=[
                        _make_evidence(award, company, source),
                    ],
                    stamp=VersionStamp(
                        data_snapshot=today.isoformat(),
                        code_version="0.0.1",
                        indicator_version=self.id,
                    ),
                )


def _get_company(company_number: str):
    from uncorrupt.staging.models import Company

    return Company.objects.filter(company_number=company_number).first()


def _confidence_note(res: dict[str, Any]) -> str:
    if res["method"] == "identifier":
        return ""
    return f" [match_confidence={res['confidence']:.1f}, method={res['method']}]"


def _fmt_value(award: Award) -> str:
    return f"{award.value_amount_cents / 100:.2f} {award.currency}"


def _make_evidence(award: Award, company, source: str) -> ProvenanceRecord:
    return ProvenanceRecord(
        source_id=source,
        source_url=award.tender_ref.source_url if award.tender_ref else "",
        retrieved_at=datetime.now(UTC),
        content_hash=hashlib.sha256(json.dumps(award.raw_json).encode()).hexdigest(),
        license="Open data",
        redistribution=Redistribution.OPEN,
        jurisdiction="GB",
        data_class=DataClass.A1,
        tier=Tier.A,
        connector="uk_contracts_finder",
        connector_version="0.1",
    )
