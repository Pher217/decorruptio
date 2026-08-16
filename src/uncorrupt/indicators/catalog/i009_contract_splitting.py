"""i009 contract_splitting — buyer carves one procurement into sub-threshold pieces.

A contracting authority that splits what should be a single advertised procurement
into several smaller awards to the SAME supplier — each below the open-tender
threshold, but summing above it, inside a short window — has (deliberately or not)
avoided the obligation to run an open, advertised tender. This is the buyer-side
analogue of the supplier-side shell indicators (i006/i007/i008): the unit is a
(buyer, supplier) cluster of awards, not a single award.

Conservative by construction — prefer false negatives over false positives:
clusters are span-bounded (anchored to their first award), any single piece that
already clears the threshold rejects the whole cluster, and only resolved suppliers
(with a Companies House company_number) are considered.

Methodology borrow from RUBLI/yangwenli's ``same_day_count`` feature, adapted to
UK Contracts Finder and Decorruptio's registry-ID-only resolution (ADR-006).
Spec approved by Opus (consult-claude.sh, 2026-08-11).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from uncorrupt.core.provenance import ProvenanceRecord, Redistribution, VersionStamp
from uncorrupt.core.tiers import DataClass, Tier
from uncorrupt.indicators.base import Flag, Indicator, ValidationStatus
from uncorrupt.indicators.catalog._framework import is_framework_or_dps
from uncorrupt.indicators.catalog._shared import confidence_note
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.staging.models import Award, AwardResolution

# Defaults. The threshold is NOT hardcoded — it is read from the locale profile
# (locales/gb.yml procedure_metadata.open_tender_threshold_gbp). min_pieces=3:
# two awards is the statistical floor for "a pattern"; matches i007's conservative
# "prefer false negatives" and i003's MIN_BUYER_AWARDS=4 floor.
DEFAULT_WINDOW_DAYS = 30
DEFAULT_MIN_PIECES = 3


@dataclass
class _AwardView:
    """A lightweight view of an award for clustering."""

    award_id: str
    tender_id: str
    award_date: date
    value_cents: int
    supplier_name: str
    company_number: str
    match_confidence: float
    match_method: str | None
    buyer_name: str | None
    procurement_method: str | None
    procurement_method_details: str | None
    title: str | None
    source_url: str
    raw_json: dict[str, Any] | None


class ContractSplitting(Indicator):
    id = "i009_contract_splitting"
    title = "Contract splitting"
    definition = (
        "A buyer awards multiple contracts to the same supplier within a short "
        "window, each below the open-tender threshold but summing above it — a "
        "pattern that may avoid the obligation to run an open, advertised tender."
    )
    inputs: list[str] = ["awards", "suppliers"]
    params: dict[str, Any] = {
        "window_days": DEFAULT_WINDOW_DAYS,
        "min_pieces": DEFAULT_MIN_PIECES,
    }
    validation: dict[str, ValidationStatus] = {
        "gb": ValidationStatus.UNVALIDATED,
        "ua": ValidationStatus.UNVALIDATED,
        "co": ValidationStatus.UNVALIDATED,
    }

    def evaluate(self, ctx: EvaluationContext) -> Iterator[Flag]:
        today = date.today()
        source = ctx.source_id

        # Threshold from the locale profile (not hardcoded — Opus spec requirement).
        threshold_gbp = ctx.locale.procedure_metadata.get("open_tender_threshold_gbp")
        if not threshold_gbp:
            # Abstain entirely if the locale doesn't carry the threshold.
            self.units_evaluated = 0
            return
        threshold_cents = int(threshold_gbp) * 100

        window_days: int = self.params.get("window_days", DEFAULT_WINDOW_DAYS)
        min_pieces: int = self.params.get("min_pieces", DEFAULT_MIN_PIECES)

        awards = (
            Award.objects.filter(source_id=source, status="active")
            .exclude(value_amount_cents__lte=0)  # B2: zero-value awards are unscoreable
            .exclude(award_date__isnull=True)
            .select_related("tender_ref", "resolution")
        )

        if awards.exists() and not AwardResolution.objects.filter(source_id=source).exists():
            raise RuntimeError(
                f"No AwardResolution rows for source '{source}' — run "
                f"resolve_suppliers('{source}') before evaluating this indicator."
            )

        # Filter to eligible awards: named, resolved supplier, non-framework, has buyer.
        # Nameless awards stay excluded (ADR-012 open item 2, pending founder decision).
        eligible: list[_AwardView] = []
        for a in awards:
            if (
                not a.supplier_name
                or not hasattr(a, "resolution")
                or not a.resolution.company_number
            ):
                continue
            r = a.resolution
            supplier_name = a.supplier_name
            company_number = r.company_number
            if supplier_name is None or company_number is None:
                continue
            tender = a.tender_ref
            if not tender or not tender.buyer_name:
                continue
            buyer: str = tender.buyer_name
            # B1: framework/DPS call-offs are NOT splitting.
            if is_framework_or_dps(
                tender.title,
                tender.procurement_method,
                tender.procurement_method_details,
                raw_json=tender.raw_json if isinstance(tender.raw_json, dict) else None,
            ):
                continue
            eligible.append(
                _AwardView(
                    award_id=a.award_id,
                    tender_id=a.tender_id,
                    award_date=a.award_date.date() if a.award_date else date.today(),
                    value_cents=a.value_amount_cents,
                    supplier_name=supplier_name,
                    company_number=company_number,
                    match_confidence=r.match_confidence,
                    match_method=r.match_method,
                    buyer_name=buyer,
                    procurement_method=tender.procurement_method,
                    procurement_method_details=tender.procurement_method_details,
                    title=tender.title,
                    source_url=tender.source_url or "",
                    raw_json=a.raw_json if isinstance(a.raw_json, dict) else None,
                )
            )

        # units_evaluated = distinct (buyer, company) groups with ≥1 eligible award.
        groups: dict[tuple[str, str], list[_AwardView]] = defaultdict(list)
        for av in eligible:
            key: tuple[str, str] = (av.buyer_name or "", av.company_number)
            groups[key].append(av)
        self.units_evaluated = len(groups)

        # Cluster + flag per group.
        for (buyer, company_number), group_awards in groups.items():
            group_awards.sort(key=lambda v: v.award_date)

            # Anchored clustering: maximal runs within window_days of the cluster's
            # first award. Conservative (span-bounded, prefers false negatives).
            clusters: list[list[_AwardView]] = []
            current: list[_AwardView] = []
            cluster_start: date | None = None
            for av in group_awards:
                if cluster_start is None:
                    current = [av]
                    cluster_start = av.award_date
                elif (av.award_date - cluster_start).days <= window_days:
                    current.append(av)
                else:
                    clusters.append(current)
                    current = [av]
                    cluster_start = av.award_date
            if current:
                clusters.append(current)

            for cluster in clusters:
                # B1: collapse into pieces by distinct tender_id.
                pieces: dict[str, int] = defaultdict(int)
                piece_awards: dict[str, list[_AwardView]] = defaultdict(list)
                for av in cluster:
                    pieces[av.tender_id] += av.value_cents
                    piece_awards[av.tender_id].append(av)

                n_pieces = len(pieces)
                if n_pieces < min_pieces:
                    continue

                piece_values = list(pieces.values())
                total_cents = sum(piece_values)

                # All pieces under threshold + sum over threshold.
                if any(v >= threshold_cents for v in piece_values):
                    continue  # a piece already cleared — not splitting
                if total_cents <= threshold_cents:
                    continue  # sum doesn't exceed — no avoidance

                # Severity band from span.
                span_days = (cluster[-1].award_date - cluster[0].award_date).days
                if span_days <= 1:
                    band = "same-day"
                elif span_days <= 7:
                    band = "same-week"
                else:
                    band = f"within-{window_days}d"

                # Confidence: weakest match in the cluster. Always printed — see
                # `_shared.confidence_note` for why suppressing it on an identifier
                # match is exactly wrong.
                weakest = min(cluster, key=lambda v: v.match_confidence)
                conf_note = (
                    f" Weakest cluster match:"
                    f"{confidence_note(weakest.match_confidence, weakest.match_method)}"
                )

                cluster_date = cluster[0].award_date.isoformat()
                subject_ref = f"{company_number}@{buyer}:{cluster_date}"

                explanation = (
                    f"Buyer '{buyer}' awarded {n_pieces} contracts to supplier "
                    f"(company {company_number}) within {span_days} days ({band}), "
                    f"each below the £{threshold_gbp:,} open-tender threshold but "
                    f"summing to £{total_cents / 100:,.2f}. "
                    f"This contract-splitting pattern may avoid open competition."
                    f"{conf_note}"
                )

                # G6: one ProvenanceRecord per constituent award.
                evidence = [
                    ProvenanceRecord(
                        source_id=source,
                        source_url=av.source_url,
                        retrieved_at=datetime.now(UTC),
                        content_hash=hashlib.sha256(
                            json.dumps(av.raw_json or {}, sort_keys=True).encode()
                        ).hexdigest(),
                        license="Open data",
                        redistribution=Redistribution.OPEN,
                        jurisdiction="GB",
                        data_class=DataClass.A1,
                        tier=Tier.A,
                        connector=source,
                        connector_version="0.1",
                    )
                    for av in cluster
                ]

                yield Flag(
                    indicator_id=self.id,
                    subject_ref=subject_ref,
                    as_of=today,
                    explanation=explanation,
                    evidence=evidence,
                    stamp=VersionStamp(
                        data_snapshot=today.isoformat(),
                        code_version="0.0.1",
                        indicator_version=self.id,
                    ),
                )
