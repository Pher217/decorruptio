"""Kill-test for i002/i003/i004 against the VIP-lane cohort (never scored before).

`findings.md` §2 reports separation for i001/i005/i006/i007/i008 only — those five
recover the procedural signature of the High Priority Lane, not corruption, and none
separates the one adjudicated case (PPE Medpro) from benign lane members. i002 (short
bid window), i003 (repeat-winner share) and i004 (price vs estimate) are marked
VALIDATED for "gb" in their catalog modules but have never been run against this
cohort. This script runs the real, unmodified catalog `Indicator` classes (not a
reimplementation) against the live staging DB, joins their output back onto the
published VIP-lane/control cohort from `scripts/cohort_test_v2.py`
(`experiments/cohort_test_v2_results.json` — reused, not re-run, per findings.md
"do not re-run"), and reports results WITHIN procedural x temporal strata, never
across them (an unconditioned cross-stratum comparison is what produced the +50.3pp
i005 headline that turned out to be a lane-detector artifact, not a corruption signal).

All database access is read-only: the join and the indicator evaluation both run
inside one `transaction.atomic()` block that is explicitly rolled back.

Usage:
    PYTHONPATH=.:src uv run python scripts/indicator_kill_test.py
    PYTHONPATH=.:src uv run python scripts/indicator_kill_test.py \
        --output experiments/indicator_kill_test_results.json
"""

from __future__ import annotations

import argparse
import json
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from django.db import transaction  # noqa: E402
from django.db.models import Count  # noqa: E402

from uncorrupt.indicators.catalog._framework import is_framework_or_dps  # noqa: E402
from uncorrupt.indicators.catalog.i002_short_bid_window import ShortBidWindow  # noqa: E402
from uncorrupt.indicators.catalog.i003_repeat_winner_share import (  # noqa: E402
    RepeatWinnerShare,
    _is_placeholder_supplier,
)
from uncorrupt.indicators.catalog.i004_price_vs_estimate import PriceVsEstimate  # noqa: E402
from uncorrupt.indicators.context import EvaluationContext  # noqa: E402
from uncorrupt.register.loader import load_locale  # noqa: E402
from uncorrupt.staging.models import Award, Tender  # noqa: E402

SOURCE_ID = "uk_contracts_finder"
COHORT_RESULTS = Path("experiments/cohort_test_v2_results.json")
DEFAULT_OUTPUT = Path("experiments/indicator_kill_test_results.json")

# Same emergency window findings.md calls out: "in March-June 2020 the entire market
# was emergency direct-award; an unconditioned 2020 indicator describes the pandemic,
# not corruption."
EMERGENCY_START = date(2020, 3, 1)
EMERGENCY_END = date(2020, 6, 30)

# Mirrors RepeatWinnerShare.MIN_BUYER_AWARDS — duplicated here only to report which
# cohort buyer-supplier pairs the indicator's own denominator floor excludes from
# ever producing a flag. Not used to decide any flag; the flag decision always comes
# from calling the real, unmodified RepeatWinnerShare.evaluate().
I003_MIN_BUYER_AWARDS = 4

PPE_MEDPRO_SUPPLIER = "PPE Medpro Ltd"  # canonical name as it appears in the VIP-lane CSV/cohort

FLAGGED = "flagged"
NOT_FLAGGED = "not_flagged"
# i004 emits BOTH above-estimate (over-pricing, the real signal) and below-estimate
# ("weak", excluded from the curated output per i004's own docstring) flags on the
# same subject_ref pattern. Conflating them would misreport i004's firing rate, so
# below-estimate hits get their own status rather than counting as FLAGGED.
FLAGGED_WEAK_BELOW_ESTIMATE = "flagged_weak_below_estimate"


# ── Pure stratification / coverage helpers (no DB — unit-testable) ─────────────────


def temporal_stratum(award_date: date) -> str:
    """Emergency (Mar-Jun 2020, near-universal direct award) vs the rest of 2020."""
    if EMERGENCY_START <= award_date <= EMERGENCY_END:
        return "emergency_mar_jun_2020"
    return "other_2020"


def procedural_stratum(method: str | None, method_details: str | None) -> str:
    """Direct-award vs competitive vs unknown, mirroring cohort_test_v2.py's check_i005
    heuristic (procedural variable used as a denominator here, never as a flag)."""
    method_l = (method or "").lower()
    details_l = (method_details or "").lower()
    if method_l == "limited":
        return "direct_award"
    if "without prior publication" in details_l:
        return "direct_award"
    if "direct" in details_l:
        return "direct_award"
    if not method_l:
        return "unknown_method"
    return "competitive"


def stratum_key(award_date: date, method: str | None, method_details: str | None) -> str:
    return f"{procedural_stratum(method, method_details)}__{temporal_stratum(award_date)}"


def score_i002_coverage(tender_start: object, tender_end: object) -> str:
    """i002 needs both bid-window endpoints. Missing either is UNSCOREABLE, not a no-flag."""
    if tender_start is None or tender_end is None:
        return "unscoreable_missing_window"
    return "scoreable"


def score_i004_coverage(tender_value_cents: int, award_value_cents: int, is_framework: bool) -> str:
    """i004 needs a positive tender estimate and a positive award value, and excludes
    framework/DPS tenders (their "estimate" is a ceiling, not a market-price estimate —
    the real indicator's own exclusion, reused here to classify coverage, not to flag)."""
    if is_framework:
        return "excluded_framework_dps"
    if tender_value_cents <= 0 or award_value_cents <= 0:
        return "unscoreable_missing_estimate"
    return "scoreable"


def score_i003_coverage(
    buyer_name: str | None, supplier_name: str | None, buyer_total_awards: int
) -> str:
    """i003 needs a real buyer, a real (non-placeholder) supplier, and a buyer with
    >= MIN_BUYER_AWARDS total awards — below that floor the pair can never be evaluated
    by the real indicator's own denominator guard."""
    if not buyer_name:
        return "unscoreable_no_buyer"
    if not supplier_name or _is_placeholder_supplier(supplier_name):
        return "unscoreable_placeholder_supplier"
    if buyer_total_awards < I003_MIN_BUYER_AWARDS:
        return "unscoreable_buyer_below_floor"
    return "scoreable"


@dataclass(frozen=True)
class JoinCandidate:
    """A plain, DB-independent view of one staging Award row, for join disambiguation."""

    award_id: str
    supplier_name: str
    value_amount_cents: int


def match_award(
    candidates: list[JoinCandidate], raw_supplier_name: str, award_value_gbp: float
) -> JoinCandidate | None:
    """Disambiguate which staging Award row a published cohort record refers to.

    A cohort JSON row (from cohort_test_v2_results.json) carries only an ocid, a
    supplier name, and an award value — no award_id. One ocid can have several Award
    rows (multi-supplier tenders), so match on exact supplier name first, falling back
    to award value (cohort value is whole GBP; staging is integer cents).
    """
    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]
    for c in candidates:
        if c.supplier_name == raw_supplier_name:
            return c
    target_cents = round(award_value_gbp * 100)
    for c in candidates:
        if c.value_amount_cents == target_cents:
            return c
    return None


@dataclass(frozen=True)
class RankEntry:
    supplier: str
    flagged_indicators: frozenset[str]
    scored_indicators: frozenset[str]


def rank_within_stratum(entries: list[RankEntry]) -> list[dict[str, Any]]:
    """Rank suppliers within one stratum by count of indicators fired, most first.

    Competition ranking (1,1,3 — not 1,1,2): ties share a rank because "ranks above"
    must mean strictly more flags. Ties broken alphabetically for a reproducible order
    (cohort_test_v2's equivalent left ties in dict-insertion order).
    """
    ordered = sorted(entries, key=lambda e: (-len(e.flagged_indicators), e.supplier))
    ranked: list[dict[str, Any]] = []
    prev_count: int | None = None
    prev_rank = 0
    for i, e in enumerate(ordered, start=1):
        count = len(e.flagged_indicators)
        rank = prev_rank if count == prev_count else i
        prev_count, prev_rank = count, rank
        ranked.append(
            {
                "rank": rank,
                "supplier": e.supplier,
                "n_flagged": count,
                "flagged_indicators": sorted(e.flagged_indicators),
                "n_scored": len(e.scored_indicators),
            }
        )
    return ranked


# ── Cohort loading + DB join (read-only) ────────────────────────────────────────────


def load_cohort() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load the published VIP-lane / control cohort. Never regenerates it — reuses
    scripts/cohort_test_v2.py's output as findings.md directs ("do not re-run")."""
    if not COHORT_RESULTS.exists():
        raise FileNotFoundError(
            f"{COHORT_RESULTS} not found. This script reuses the published cohort "
            "construction (findings.md #2: 108 VIP-lane / 2,423 control awards) rather "
            "than re-running scripts/cohort_test_v2.py. Populate "
            f"{COHORT_RESULTS} from a prior run before invoking this script."
        )
    data = json.loads(COHORT_RESULTS.read_text())
    return data["vip_results"], data["control_results"]


def _join_one(
    record: dict[str, Any],
    tender_by_ocid: dict[str, Tender],
    awards_by_ocid: dict[str, list[Award]],
) -> dict[str, Any]:
    ocid = record["ocid"]
    tender = tender_by_ocid.get(ocid)
    if tender is None:
        return {**record, "join_status": "tender_missing", "award": None, "tender": None}

    cands = [
        JoinCandidate(a.award_id, a.supplier_name or "", a.value_amount_cents)
        for a in awards_by_ocid.get(ocid, [])
    ]
    match = match_award(cands, record.get("raw_supplier_name", ""), record["award_value_gbp"])
    if match is None:
        return {**record, "join_status": "award_not_matched", "award": None, "tender": tender}

    award = next(a for a in awards_by_ocid[ocid] if a.award_id == match.award_id)
    return {**record, "join_status": "matched", "award": award, "tender": tender}


def join_cohort_to_staging(
    vip: list[dict[str, Any]], control: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join the published cohort's (ocid, supplier, value) rows onto staging Tender/Award.

    Must run inside the caller's transaction.atomic() block — this function issues
    read queries only and returns plain joined records.
    """
    all_ocids = {r["ocid"] for r in vip} | {r["ocid"] for r in control}

    tenders = Tender.objects.filter(source_id=SOURCE_ID, ocid__in=all_ocids)
    tender_by_ocid = {t.ocid: t for t in tenders}

    awards = Award.objects.filter(
        source_id=SOURCE_ID, tender_ref__ocid__in=all_ocids
    ).select_related("tender_ref")
    awards_by_ocid: dict[str, list[Award]] = defaultdict(list)
    for a in awards:
        awards_by_ocid[a.tender_ref.ocid].append(a)

    vip_joined = [_join_one(r, tender_by_ocid, awards_by_ocid) for r in vip]
    control_joined = [_join_one(r, tender_by_ocid, awards_by_ocid) for r in control]
    return vip_joined, control_joined


@dataclass(frozen=True)
class IndicatorRun:
    units_evaluated: dict[str, int]
    flags: dict[str, set[str]]
    i004_direction: dict[str, str]  # subject_ref -> "above" | "below" (weak)
    buyer_totals: dict[str, int]


def run_real_indicators() -> IndicatorRun:
    """Run the real, unmodified catalog Indicator classes once against the whole
    "gb" corpus — exactly how they run in production. Must run inside the caller's
    transaction.atomic() block."""
    locale = load_locale("gb")
    ctx = EvaluationContext(locale=locale, source_id=SOURCE_ID)

    i002 = ShortBidWindow()
    i002_flags = {f.subject_ref for f in i002.evaluate(ctx)}

    i003 = RepeatWinnerShare()
    i003_flags = {f.subject_ref for f in i003.evaluate(ctx)}

    i004 = PriceVsEstimate()
    i004_raw_flags = list(i004.evaluate(ctx))
    i004_flags = {f.subject_ref for f in i004_raw_flags}
    # "[WEAK: below-estimate]" is the exact marker PriceVsEstimate.evaluate() writes
    # into the explanation for below-estimate hits — reused here read-only to sort
    # flags into the two directions the indicator's own code already distinguishes.
    i004_direction = {
        f.subject_ref: ("below" if "[WEAK: below-estimate]" in f.explanation else "above")
        for f in i004_raw_flags
    }

    # i003's own buyer-total denominator, recomputed here read-only purely to report
    # coverage (which cohort pairs sit below MIN_BUYER_AWARDS). The flag decision
    # above always comes from RepeatWinnerShare.evaluate(), never from this.
    buyer_totals: dict[str, int] = defaultdict(int)
    pairs = (
        Award.objects.filter(
            source_id=SOURCE_ID,
            supplier_name__isnull=False,
            tender_ref__buyer_name__isnull=False,
        )
        .values("tender_ref__buyer_name", "supplier_name")
        .annotate(n=Count("id"))
    )
    for row in pairs:
        if _is_placeholder_supplier(row["supplier_name"] or ""):
            continue
        buyer_totals[row["tender_ref__buyer_name"]] += row["n"]

    return IndicatorRun(
        units_evaluated={
            "i002": i002.units_evaluated,
            "i003": i003.units_evaluated,
            "i004": i004.units_evaluated,
        },
        flags={"i002": i002_flags, "i003": i003_flags, "i004": i004_flags},
        i004_direction=i004_direction,
        buyer_totals=dict(buyer_totals),
    )


def score_record(record: dict[str, Any], run: IndicatorRun) -> dict[str, Any]:
    """Attach stratum + per-indicator status to one joined cohort record.

    Extracts only plain scalars from the Award/Tender ORM objects so the result is
    safe to use after the enclosing transaction has rolled back.
    """
    out: dict[str, Any] = {
        "cohort": record["cohort"],
        "supplier_name": record["supplier_name"],
        "ocid": record["ocid"],
        "award_value_gbp": record["award_value_gbp"],
        "join_status": record["join_status"],
    }

    if record["join_status"] != "matched":
        out["stratum"] = None
        out["i002_status"] = "unscoreable_not_joined"
        out["i003_status"] = "unscoreable_not_joined"
        out["i004_status"] = "unscoreable_not_joined"
        return out

    award: Award = record["award"]
    tender: Tender = record["tender"]

    out["tender_id"] = award.tender_id
    out["award_id"] = award.award_id
    out["buyer_name"] = tender.buyer_name
    out["procurement_method"] = tender.procurement_method
    out["procurement_method_details"] = tender.procurement_method_details

    award_date = award.award_date.date() if award.award_date else None
    out["award_date"] = award_date.isoformat() if award_date else None
    out["stratum"] = (
        stratum_key(award_date, tender.procurement_method, tender.procurement_method_details)
        if award_date
        else "unknown_date"
    )

    # i002
    cov = score_i002_coverage(tender.tender_start, tender.tender_end)
    if cov == "scoreable":
        out["i002_status"] = FLAGGED if tender.tender_id in run.flags["i002"] else NOT_FLAGGED
    else:
        out["i002_status"] = cov

    # i004
    is_fw = is_framework_or_dps(
        tender.title, tender.procurement_method, tender.procurement_method_details, tender.raw_json
    )
    cov = score_i004_coverage(tender.value_amount_cents, award.value_amount_cents, is_fw)
    if cov == "scoreable":
        subject = f"{award.tender_id}:{award.award_id}"
        if subject not in run.flags["i004"]:
            out["i004_status"] = NOT_FLAGGED
        elif run.i004_direction.get(subject) == "below":
            out["i004_status"] = FLAGGED_WEAK_BELOW_ESTIMATE
        else:
            out["i004_status"] = FLAGGED
    else:
        out["i004_status"] = cov

    # i003
    buyer = tender.buyer_name
    supplier = award.supplier_name
    total = run.buyer_totals.get(buyer or "", 0)
    cov = score_i003_coverage(buyer, supplier, total)
    if cov == "scoreable":
        subject = f"{buyer}→{supplier}"
        out["i003_status"] = FLAGGED if subject in run.flags["i003"] else NOT_FLAGGED
    else:
        out["i003_status"] = cov

    return out


# ── Reporting ────────────────────────────────────────────────────────────────────────


INDICATORS = ("i002", "i003", "i004")


def field_coverage_table(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    table: dict[str, dict[str, int]] = {ind: defaultdict(int) for ind in INDICATORS}
    for r in records:
        for ind in INDICATORS:
            table[ind][r[f"{ind}_status"]] += 1
    return {ind: dict(counts) for ind, counts in table.items()}


def per_stratum_firing_rates(records: list[dict[str, Any]]) -> dict[str, dict[str, dict[str, Any]]]:
    """For each stratum x indicator, the firing rate among SCOREABLE population members
    (VIP + control combined — control is already ~all non-VIP PPE awards in the cohort
    construction, so this is the best available population proxy)."""
    scoreable = [r for r in records if r["stratum"] is not None]
    strata = sorted({r["stratum"] for r in scoreable})

    out: dict[str, dict[str, dict[str, Any]]] = {}
    for stratum in strata:
        stratum_records = [r for r in scoreable if r["stratum"] == stratum]
        out[stratum] = {}
        for ind in INDICATORS:
            scored = [
                r
                for r in stratum_records
                if r[f"{ind}_status"] in (FLAGGED, NOT_FLAGGED, FLAGGED_WEAK_BELOW_ESTIMATE)
            ]
            # Below-estimate i004 hits are excluded from the curated output by the
            # indicator's own design (see FLAGGED_WEAK_BELOW_ESTIMATE) — count them as
            # scoreable-but-not-flagged, not as a real flag, for firing-rate purposes.
            flagged = [r for r in scored if r[f"{ind}_status"] == FLAGGED]
            n_scoreable = len(scored)
            rate = len(flagged) / n_scoreable if n_scoreable else None
            out[stratum][ind] = {
                "population": len(stratum_records),
                "scoreable": n_scoreable,
                "flagged": len(flagged),
                "firing_rate": rate,
                "is_stratifier": rate is not None and rate > 0.20,
            }
    return out


def ppe_medpro_rank(vip_scored: list[dict[str, Any]]) -> dict[str, Any]:
    """Does any indicator rank PPE Medpro above matched benign VIP-lane members within
    its own stratum? The acceptance test the whole exercise turns on."""
    medpro_awards = [r for r in vip_scored if r["supplier_name"] == PPE_MEDPRO_SUPPLIER]
    medpro_joined = [r for r in medpro_awards if r["stratum"] is not None]

    if not medpro_joined:
        return {
            "joinable": False,
            "n_known_awards": len(medpro_awards),
            "n_joined_awards": 0,
            "note": "None of PPE Medpro's known VIP-lane awards could be joined to the "
            "currently-loaded staging DB — no rank can be computed.",
        }

    per_stratum: dict[str, Any] = {}
    for stratum in {r["stratum"] for r in medpro_joined}:
        peers = [r for r in vip_scored if r["stratum"] == stratum]
        entries = defaultdict(lambda: {"flagged": set(), "scored": set()})
        for r in peers:
            e = entries[r["supplier_name"]]
            for ind in INDICATORS:
                status = r[f"{ind}_status"]
                if status == FLAGGED:
                    e["flagged"].add(ind)
                    e["scored"].add(ind)
                elif status in (NOT_FLAGGED, FLAGGED_WEAK_BELOW_ESTIMATE):
                    e["scored"].add(ind)
        rank_entries = [
            RankEntry(
                supplier=s,
                flagged_indicators=frozenset(v["flagged"]),
                scored_indicators=frozenset(v["scored"]),
            )
            for s, v in entries.items()
        ]
        ranked = rank_within_stratum(rank_entries)
        medpro_row = next(row for row in ranked if row["supplier"] == PPE_MEDPRO_SUPPLIER)
        per_stratum[stratum] = {
            "n_peers": len(ranked),
            "medpro_rank": medpro_row["rank"],
            "medpro_flagged": medpro_row["flagged_indicators"],
            "medpro_n_scored": medpro_row["n_scored"],
            "beats_all_peers": medpro_row["rank"] == 1
            and medpro_row["n_flagged"] > 0
            and all(
                row["n_flagged"] < medpro_row["n_flagged"]
                for row in ranked
                if row["supplier"] != PPE_MEDPRO_SUPPLIER
            ),
            "full_ranking": ranked,
        }

    return {
        "joinable": True,
        "n_known_awards": len(medpro_awards),
        "n_joined_awards": len(medpro_joined),
        "per_stratum": per_stratum,
    }


def build_report(
    vip_scored: list[dict[str, Any]], control_scored: list[dict[str, Any]]
) -> dict[str, Any]:
    all_records = vip_scored + control_scored
    join_counts = {
        "vip": {
            status: sum(1 for r in vip_scored if r["join_status"] == status)
            for status in {r["join_status"] for r in vip_scored}
        },
        "control": {
            status: sum(1 for r in control_scored if r["join_status"] == status)
            for status in {r["join_status"] for r in control_scored}
        },
    }
    return {
        "cohort_sizes": {
            "vip_published": len(vip_scored),
            "control_published": len(control_scored),
        },
        "join_status": join_counts,
        "field_coverage": field_coverage_table(all_records),
        "per_stratum_firing_rates": per_stratum_firing_rates(all_records),
        "ppe_medpro": ppe_medpro_rank(vip_scored),
    }


def print_report(report: dict[str, Any]) -> None:
    print("=" * 78)
    print("INDICATOR KILL-TEST — i002 / i003 / i004 vs the VIP-lane cohort")
    print("=" * 78)

    print("\n## COHORT (published, reused from scripts/cohort_test_v2.py)")
    print(f"  VIP-lane awards: {report['cohort_sizes']['vip_published']}")
    print(f"  Control awards:  {report['cohort_sizes']['control_published']}")

    print("\n## JOIN TO LIVE STAGING DB (bounds what can even be scored)")
    for cohort in ("vip", "control"):
        print(f"  {cohort}: {report['join_status'][cohort]}")

    print("\n## FIELD COVERAGE PER INDICATOR (across all joined cohort awards)")
    for ind, counts in report["field_coverage"].items():
        total = sum(counts.values())
        scoreable = (
            counts.get(FLAGGED, 0)
            + counts.get(NOT_FLAGGED, 0)
            + counts.get(FLAGGED_WEAK_BELOW_ESTIMATE, 0)
        )
        print(f"  {ind}: {scoreable}/{total} scoreable  {counts}")

    print("\n## PER-STRATUM FIRING RATES (VIP + control combined; >20-30% = stratifier, not flag)")
    for stratum, inds in report["per_stratum_firing_rates"].items():
        print(f"\n  stratum: {stratum}")
        for ind, stats in inds.items():
            rate = stats["firing_rate"]
            rate_str = f"{rate:.1%}" if rate is not None else "N/A (0 scoreable)"
            flag = "  <-- STRATIFIER, not a flag" if stats["is_stratifier"] else ""
            print(
                f"    {ind}: {stats['flagged']}/{stats['scoreable']} scoreable "
                f"({stats['population']} in stratum) = {rate_str}{flag}"
            )

    print("\n## PPE MEDPRO — WITHIN-STRATUM RANK (the acceptance test)")
    medpro = report["ppe_medpro"]
    if not medpro["joinable"]:
        print(f"  {medpro['note']}")
    else:
        print(
            f"  {medpro['n_joined_awards']}/{medpro['n_known_awards']} known VIP-lane "
            "awards joinable to staging"
        )
        for stratum, info in medpro["per_stratum"].items():
            print(f"\n  stratum: {stratum} ({info['n_peers']} VIP-lane suppliers)")
            print(f"    PPE Medpro rank: {info['medpro_rank']}/{info['n_peers']}")
            print(
                f"    PPE Medpro flagged by: {info['medpro_flagged'] or 'none'} "
                f"(of {info['medpro_n_scored']} scoreable)"
            )
            print(f"    Separates Medpro from every benign peer: {info['beats_all_peers']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    args = parser.parse_args()

    vip, control = load_cohort()

    with transaction.atomic():
        vip_joined, control_joined = join_cohort_to_staging(vip, control)
        run = run_real_indicators()
        vip_scored = [score_record(r, run) for r in vip_joined]
        control_scored = [score_record(r, run) for r in control_joined]
        transaction.set_rollback(True)

    report = build_report(vip_scored, control_scored)
    print_report(report)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nFull report saved to {output_path}")


if __name__ == "__main__":
    main()
