"""i007 value_vs_company_size — award value large relative to company accounts category.

PestFix case: a small pest-control firm won ~GBP 340m in PPE contracts.
The company's accounts category (dormant / micro-entity / small) indicates its
operational scale. An award dwarfing that scale is a red flag — the company
may lack capacity to deliver, or the contract may have been directed to a
vehicle that was never meant to perform.

Conservative thresholds — prefer false negatives over false positives.
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

# Thresholds: award value (in GBP cents) above which a small/dormant company
# is suspicious. These are conservative — set high to avoid false positives.
#
# Accounts categories in Companies House:
#   DORMANT      — company is not trading; accounts show no significant activity
#   MICRO_ENTITY — very small company (turnover < £632k, balance sheet < £316k)
#   SMALL       — small company (turnover < £10.2m, balance sheet < £5.1m)
#
# A dormant company winning ANY award is suspicious.
# A micro-entity winning > £500k is suspicious (larger than its balance sheet ceiling).
# A small company winning > £5m is suspicious (half its turnover ceiling).
DORMANT_THRESHOLD_CENTS = 0  # any award to a dormant company
MICRO_ENTITY_THRESHOLD_CENTS = 50_000_00  # £50,000
SMALL_THRESHOLD_CENTS = 5_000_000_00  # £5,000,000

# Accounts category values in CH bulk data (case-insensitive matching)
DORMANT_CATEGORIES = {"dormant", "dormant company", "dormant no significant accounting"}
MICRO_CATEGORIES = {"micro-entity", "micro entity"}
SMALL_CATEGORIES = {"small", "small company"}


class ValueVsCompanySize(Indicator):
    id = "i007_value_vs_company_size"
    title = "Value vs company size"
    definition = (
        "Award value large relative to the company's accounts category "
        "(dormant / micro-entity / small). The PestFix indicator."
    )
    inputs: list[str] = ["awards", "companies"]
    params: dict[str, Any] = {
        "dormant_threshold_cents": DORMANT_THRESHOLD_CENTS,
        "micro_entity_threshold_cents": MICRO_ENTITY_THRESHOLD_CENTS,
        "small_threshold_cents": SMALL_THRESHOLD_CENTS,
    }
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

        resolutions: dict[str, dict[str, Any]] = {}
        for res in SupplierResolution.objects.filter(source_id=source).exclude(
            company_number__isnull=True
        ):
            resolutions[res.supplier_name] = {
                "company_number": res.company_number,
                "confidence": res.match_confidence,
                "method": res.match_method,
            }

        evaluable = [a for a in awards if a.supplier_name and a.supplier_name in resolutions]
        self.units_evaluated = len(evaluable)

        for award in evaluable:
            res = resolutions[award.supplier_name]
            company = _get_company(res["company_number"])
            if not company or not company.accounts_category:
                continue

            category = company.accounts_category.lower().strip()
            value_cents = award.value_amount_cents
            confidence_note = _confidence_note(res)

            flag_reason = None
            threshold = 0

            if category in DORMANT_CATEGORIES and value_cents > DORMANT_THRESHOLD_CENTS:
                flag_reason = "dormant accounts"
                threshold = DORMANT_THRESHOLD_CENTS
            elif category in MICRO_CATEGORIES and value_cents > MICRO_ENTITY_THRESHOLD_CENTS:
                flag_reason = "micro-entity accounts"
                threshold = MICRO_ENTITY_THRESHOLD_CENTS
            elif category in SMALL_CATEGORIES and value_cents > SMALL_THRESHOLD_CENTS:
                flag_reason = "small company accounts"
                threshold = SMALL_THRESHOLD_CENTS

            if flag_reason:
                yield Flag(
                    indicator_id=self.id,
                    subject_ref=f"{award.tender_id}:{award.award_id}",
                    as_of=today,
                    explanation=(
                        f"Supplier '{award.supplier_name}' with {flag_reason} "
                        f"(category: '{company.accounts_category}') won award of "
                        f"{_fmt_value(award)} on {_award_date_str(award)}."
                        f" Threshold: awards above {_fmt_cents(threshold)} to {flag_reason} "
                        f"companies are flagged. Company number {company.company_number}. "
                        f"A company this small winning an award this large may lack capacity "
                        f"to deliver, or the contract may have been directed.{confidence_note}"
                    ),
                    evidence=[_make_evidence(award, company, source)],
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


def _fmt_cents(cents: int) -> str:
    return f"{cents / 100:.2f}"


def _award_date_str(award: Award) -> str:
    return award.award_date.date().isoformat() if award.award_date else "unknown"


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
