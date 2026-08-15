"""i008 dormancy_delinquency — dormant accounts or overdue/stale filings at award date.

A company with dormant accounts or very stale filings winning a contract is a red flag:
it suggests the company was not actively trading before the award, or is failing
to meet its statutory filing obligations.
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
from uncorrupt.indicators.catalog._shared import confidence_note
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.staging.models import Award, AwardResolution

# Accounts category values indicating dormancy
DORMANT_CATEGORIES = {"dormant", "dormant company", "dormant no significant accounting"}

# Stale filing threshold: accounts last made up date more than 18 months before
# the award date. UK companies must file annually; 18 months allows margin for
# small-company filing exemptions and late filings.
STALE_FILING_MONTHS = 18


class DormancyDelinquency(Indicator):
    id = "i008_dormancy_delinquency"
    title = "Dormancy delinquency"
    definition = (
        "Supplier with dormant accounts or overdue/very stale filings at award date. "
        "Suggests the company was not actively trading or is failing statutory obligations."
    )
    inputs: list[str] = ["awards", "companies"]
    params: dict[str, Any] = {"stale_filing_months": STALE_FILING_MONTHS}
    validation: dict[str, ValidationStatus] = {
        "gb": ValidationStatus.VALIDATED,
        "ua": ValidationStatus.UNVALIDATED,
        "co": ValidationStatus.UNVALIDATED,
    }

    def evaluate(self, ctx: EvaluationContext) -> Iterator[Flag]:
        today = date.today()
        source = ctx.source_id

        awards = Award.objects.filter(source_id=source, status="active").select_related(
            "tender_ref", "resolution"
        )

        if awards.exists() and not AwardResolution.objects.filter(source_id=source).exists():
            raise RuntimeError(
                f"No AwardResolution rows for source '{source}' — run "
                f"resolve_suppliers('{source}') before evaluating this indicator."
            )

        # Nameless awards stay excluded (ADR-012 open item 2, pending founder decision).
        evaluable = [
            a
            for a in awards
            if a.supplier_name and hasattr(a, "resolution") and a.resolution.company_number
        ]
        self.units_evaluated = len(evaluable)

        for award in evaluable:
            r = award.resolution
            company_number = r.company_number
            assert company_number is not None
            company = _get_company(company_number)
            if not company:
                continue

            flag_reason = None
            note = confidence_note(r.match_confidence, r.match_method)

            # Check for dormant accounts
            if (
                company.accounts_category
                and company.accounts_category.lower().strip() in DORMANT_CATEGORIES
            ):
                flag_reason = f"dormant accounts (category: '{company.accounts_category}')"

            # Check for stale/overdue filings
            if company.accounts_last_made_up_date and award.award_date:
                filing_age_days = (
                    award.award_date.date() - company.accounts_last_made_up_date
                ).days
                stale_threshold_days = STALE_FILING_MONTHS * 30.44  # avg month length
                if filing_age_days > stale_threshold_days:
                    months_stale = filing_age_days // 30
                    stale_reason = (
                        f"stale filings (last accounts made up to "
                        f"{company.accounts_last_made_up_date.isoformat()}, "
                        f"{months_stale} months before award)"
                    )
                    flag_reason = f"{flag_reason}; {stale_reason}" if flag_reason else stale_reason

            if flag_reason:
                yield Flag(
                    indicator_id=self.id,
                    subject_ref=f"{award.tender_id}:{award.award_id}",
                    as_of=today,
                    explanation=(
                        f"Supplier '{award.supplier_name}' had {flag_reason} at award date"
                        f" ({_award_date_str(award)}). "
                        f"Company number {company.company_number}. "
                        f"A company with dormant or delinquent filings winning a contract "
                        f"may be a shell or front entity.{note}"
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


def _award_date_str(award: Award) -> str:
    return award.award_date.date().isoformat() if award.award_date else "unknown"


def _make_evidence(award: Award, company, source: str) -> ProvenanceRecord:
    return ProvenanceRecord(
        source_id=source,
        source_url=award.tender_ref.source_url if award.tender_ref else "",
        retrieved_at=datetime.now(UTC),
        content_hash=hashlib.sha256(
            json.dumps(award.raw_json, sort_keys=True).encode()
        ).hexdigest(),
        license="Open data",
        redistribution=Redistribution.OPEN,
        jurisdiction="GB",
        data_class=DataClass.A1,
        tier=Tier.A,
        connector="uk_contracts_finder",
        connector_version="0.1",
    )
