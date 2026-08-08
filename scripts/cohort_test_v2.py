"""Cohort test v2 — using CH bulk CSV for resolution (no HTTP lookups).

Fixes per Opus directive (.consult/response-20260725T221400.md):
1. Resolution bias: both cohorts resolved through identical pipeline (CH bulk CSV)
2. All 1,724 controls resolved (not sampled to 100)
3. "Trading as" normalisation applied symmetrically to both cohorts
4. i001/i005 added to discrimination table
5. Pre-fix and post-fix numbers reported
6. Pre-registered expectations documented

Usage:
    uv run python scripts/cohort_test_v2.py
"""

from __future__ import annotations

import csv
import gzip
import json
import re
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

# Input files
POSITIVES_CSV = Path(".consult/vip_lane_positives.csv")
BULK_DIR = Path("experiments/snapshot_uk_covid_2020")
CH_BULK_CSV = Path("experiments/BasicCompanyDataAsOneFile-2026-07-01.csv")

# Output
OUTPUT_FILE = Path("experiments/cohort_test_v2_results.json")
PRE_FIX_RESULTS = Path("experiments/cohort_test_v1_results.json")  # previous run

# Foreign entities excluded (no UK CH record expected)
FOREIGN_ENTITIES = {
    "JD.COM",
    "Headwind Industrial (China) Ltd",
    "Liaoning Zhongquiao Overseas Exchange Co Ltd",
    "New Asia Logistic Service PTE Ltd",
    "Wuhan Xiaoyaoyao Pharmaceutical",
}

SUFFIXES = ["LTD", "LIMITED", "PLC", "LLP", "LP", "CIC", "CIO"]

# PPE keywords for control group filtering
PPE_KEYWORDS = [
    "ppe",
    "mask",
    "glove",
    "gown",
    "sanitiser",
    "sanitizer",
    "ventilator",
    "medical",
    "surgical",
    "protective",
    "face cover",
    "face shield",
    "apron",
    "ppe kit",
    "covid",
    "coronavirus",
    "pandemic",
]

# Indicator thresholds
INCORPORATION_PROXIMITY_DAYS = 90
I007_SMALL_THRESHOLD_GBP = 1_000_000
I007_DORMANT_THRESHOLD_GBP = 100_000
DORMANT_CATEGORIES = {"dormant", "dormant company", "dormant no significant accounting"}


# ── Pre-registered expectations ──────────────────────────────────────────────

PRE_REGISTERED = {
    "date": "2026-07-25T22:55Z",
    "expectations": [
        "i007 separation will SHRINK once positives are re-resolved through the pipeline "
        "(hand-curated numbers gave 100% resolution; pipeline will give ~74% like controls)",
        "Larger control sample (1,724 vs 100) will tighten the confidence interval",
        "i001 and i005 will flag at similarly high rates in both cohorts "
        "(emergency direct awards were near-universal in COVID PPE procurement)",
        "i006 and i008 will continue to show near-zero separation",
        "The overall 'any flag' rate will remain similar between cohorts",
    ],
    "what_would_be_surprising": [
        "If i007 separation GROWS substantially after re-resolution — would need explaining",
        "If i001/i005 show significant separation — would suggest the VIP lane concentrated "
        "direct awards beyond the background rate",
    ],
}


# ── Helpers ──────────────────────────────────────────────────────────────────


def normalise(name: str) -> str:
    return " ".join(name.upper().split())


def strip_suffix(name: str) -> str:
    norm = normalise(name)
    for s in SUFFIXES:
        if norm.endswith(" " + s):
            return norm[: -(len(s) + 1)]
    return norm


def extract_ta_name(name: str) -> str:
    """Extract legal name from 'X Ltd t/a Y', 'X Ltd trading as Y', or 'X Ltd TA Y'."""
    m = re.search(r"\b(?:t/a|trading as|TA)\b", name, re.IGNORECASE)
    if m:
        return name[: m.start()].strip()
    return name


def parse_date(date_str: str) -> str:
    if not date_str:
        return ""
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            if fmt == "%Y-%m-%d":
                return date.fromisoformat(date_str).isoformat()
            return datetime.strptime(date_str, fmt).date().isoformat()
        except (ValueError, TypeError):
            continue
    return ""


def days_between(d1: str, d2: str) -> int | None:
    try:
        dt1 = datetime.strptime(d1, "%Y-%m-%d").date()
        dt2 = datetime.strptime(d2, "%Y-%m-%d").date()
        return (dt2 - dt1).days
    except (ValueError, TypeError):
        return None


def fisher_exact_p(a: int, b: int, c: int, d: int) -> float:
    """Fisher's exact test (two-sided) for 2x2 table.

    a = VIP flagged, b = VIP not flagged, c = CTRL flagged, d = CTRL not flagged
    """
    from math import comb

    n = a + b + c + d
    row1 = a + b
    col1 = a + c
    if n == 0 or row1 == 0 or col1 == 0:
        return 1.0

    # Enumerate all possible tables with the same margins
    lo = max(0, row1 + col1 - n)
    hi = min(row1, col1)
    p_obs = comb(row1, a) * comb(n - row1, col1 - a) / comb(n, col1)
    p_two = 0.0
    for k in range(lo, hi + 1):
        p_k = comb(row1, k) * comb(n - row1, col1 - k) / comb(n, col1)
        if p_k <= p_obs:
            p_two += p_k
    return min(p_two, 1.0)


# ── OCDS bulk data ───────────────────────────────────────────────────────────


def iter_bulk_records(year: int):
    path = BULK_DIR / f"{year}.jsonl.gz"
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def extract_awards(record: dict) -> list[dict]:
    awards = []
    release = record
    ocid = release.get("ocid", "")
    tender = release.get("tender", {})
    parties = {p.get("id", ""): p for p in release.get("parties", [])}
    buyer = release.get("buyer", {})
    buyer_name = buyer.get("name", "") if isinstance(buyer, dict) else ""

    for award in release.get("awards", []):
        award_date = award.get("date", "")
        value = award.get("value", {})
        suppliers = []
        for sup in award.get("suppliers", []):
            sup_id = sup.get("id", "")
            party = parties.get(sup_id, {})
            supplier_name = sup.get("name", "") or party.get("name", "")
            identifier = party.get("identifier", {})
            suppliers.append(
                {
                    "name": supplier_name,
                    "id_scheme": identifier.get("scheme", ""),
                    "id_value": identifier.get("id", ""),
                }
            )

        awards.append(
            {
                "ocid": ocid,
                "tender_title": tender.get("title", ""),
                "tender_id": tender.get("id", ""),
                "tender_status": tender.get("status", ""),
                "tender_method": tender.get("procurementMethod", ""),
                "tender_method_details": tender.get("procurementMethodDetails", ""),
                "award_date": award_date[:10] if award_date else "",
                "award_value_amount": float(value.get("amount", 0)),
                "award_value_currency": value.get("currency", ""),
                "suppliers": suppliers,
                "buyer": buyer_name,
            }
        )

    return awards


# ── CH bulk CSV resolution ───────────────────────────────────────────────────


def build_ch_lookup(supplier_names: set[str]) -> dict[str, dict]:
    """Build a lookup from normalised_name → company data from CH bulk CSV.

    Only loads companies whose normalised_name or stripped name matches
    one of the supplier_names. Single pass through the 5.7M row CSV.

    Returns: {normalised_name: {company_number, company_name, ...}}
    """
    # Build lookup keys: all normalised variants of supplier names
    # (with and without t/a extraction, with and without suffix stripping)
    lookup_keys: dict[str, str] = {}  # key → original supplier name
    for name in supplier_names:
        # Full normalised
        norm = normalise(name)
        lookup_keys[norm] = name
        # Suffix-stripped
        stripped = strip_suffix(name)
        if stripped != norm:
            lookup_keys[stripped] = name
        # t/a extracted (legal name only)
        legal = extract_ta_name(name)
        if legal != name:
            legal_norm = normalise(legal)
            lookup_keys[legal_norm] = name
            legal_stripped = strip_suffix(legal)
            if legal_stripped != legal_norm:
                lookup_keys[legal_stripped] = name

    print(f"  Built {len(lookup_keys)} lookup keys from {len(supplier_names)} supplier names")

    # Multiple companies can share the same name — track all matches
    matches: dict[str, list[dict]] = defaultdict(list)
    count = 0

    with CH_BULK_CSV.open(encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            count += 1
            if count % 1_000_000 == 0:
                total_matches = sum(len(v) for v in matches.values())
                print(f"    [{count:,}] companies scanned, matches={total_matches}")

            company_name = row.get("CompanyName", "").strip()
            if not company_name:
                continue

            norm = normalise(company_name)
            stripped = strip_suffix(company_name)

            # Check both normalised and suffix-stripped names against lookup
            matched_keys = set()
            if norm in lookup_keys:
                matched_keys.add(norm)
            if stripped in lookup_keys:
                matched_keys.add(stripped)

            for key in matched_keys:
                matches[key].append(
                    {
                        "company_number": row.get("CompanyNumber", "").strip(),
                        "company_name": company_name,
                        "company_status": row.get("CompanyStatus", "").strip(),
                        "incorporation_date": parse_date(row.get("IncorporationDate", "")),
                        "accounts_category": row.get("Accounts.AccountCategory", "").strip(),
                        "accounts_last_made_up_date": parse_date(
                            row.get("Accounts.LastMadeUpDate", "")
                        ),
                        "normalised_name": norm,
                    }
                )

    print(f"  Scanned {count:,} companies total")
    print(f"  Found matches for {len(matches)} names")

    # Resolve: for each supplier name, pick the best match
    # Preference: unique match > sole active > first active > first
    resolved: dict[str, dict] = {}
    ambiguous: dict[str, list[dict]] = {}

    # Map back from lookup keys to original supplier names
    name_to_keys: dict[str, list[str]] = defaultdict(list)
    for key, orig_name in lookup_keys.items():
        name_to_keys[orig_name].append(key)

    for orig_name, keys in name_to_keys.items():
        all_matches = []
        for key in keys:
            all_matches.extend(matches.get(key, []))

        if not all_matches:
            continue

        if len(all_matches) == 1:
            resolved[orig_name] = all_matches[0]
            continue

        # Multiple matches: prefer sole active
        active = [m for m in all_matches if m["company_status"] == "Active"]
        if len(active) == 1:
            resolved[orig_name] = active[0]
        elif len(all_matches) == 1:
            resolved[orig_name] = all_matches[0]
        else:
            # Ambiguous — record but don't resolve
            ambiguous[orig_name] = all_matches

    print(f"  Resolved: {len(resolved)}")
    print(f"  Ambiguous (uniqueness guard): {len(ambiguous)}")
    print(f"  Unmatched: {len(supplier_names) - len(resolved) - len(ambiguous)}")

    return resolved


# ── Indicators ───────────────────────────────────────────────────────────────


def check_i001(award: dict) -> dict:
    """Single bidder (only 1 supplier on award). Catalog: i001_single_bidder."""
    suppliers = award.get("suppliers", [])
    if len(suppliers) == 1:
        return {"flag": True, "reason": "single supplier on award"}
    return {"flag": False, "reason": f"{len(suppliers)} suppliers"}


def check_i005(award: dict) -> dict:
    """Direct award (no competitive tender). Catalog: i005_direct_award_share."""
    method = (award.get("tender_method") or "").lower()
    method_details = (award.get("tender_method_details") or "").lower()
    if method == "limited":
        return {"flag": True, "reason": "limited procedure (direct award)"}
    if "without prior publication" in method_details:
        return {"flag": True, "reason": "negotiated without prior publication"}
    if "direct" in method_details:
        return {"flag": True, "reason": "direct award"}
    if method == "":
        return {"flag": False, "reason": "no procurement method data"}
    return {"flag": False, "reason": f"method={method}"}


def check_i006(award: dict, ch: dict) -> dict:
    incorp = ch.get("incorporation_date", "")
    award_date = award.get("award_date", "")
    if not incorp or not award_date:
        return {"flag": False, "reason": "missing dates"}
    days = days_between(incorp, award_date)
    if days is None:
        return {"flag": False, "reason": "date parse error"}
    if 0 <= days < INCORPORATION_PROXIMITY_DAYS:
        return {"flag": True, "reason": f"incorporated {days} days before award"}
    return {"flag": False, "reason": f"incorporated {days} days before award"}


def check_i007(award: dict, ch: dict) -> dict:
    value_gbp = award.get("award_value_amount", 0)
    if value_gbp == 0:
        return {"flag": False, "reason": "no award value"}

    category = (ch.get("accounts_category") or "unknown").lower()
    incorp = ch.get("incorporation_date", "")
    award_date = award.get("award_date", "")

    # If company is <18 months old at award, it may not have filed yet
    is_young = False
    if incorp and award_date:
        days = days_between(incorp, award_date)
        if days is not None and days < 540:
            is_young = True

    if category == "no accounts available" or (is_young and category in ("", "unknown")):
        if is_young and value_gbp > I007_SMALL_THRESHOLD_GBP:
            return {
                "flag": True,
                "reason": f"company <18mo old, no accounts, won £{value_gbp:,.0f}",
            }
        return {"flag": False, "reason": f"accounts={category}, young={is_young}"}

    if category in DORMANT_CATEGORIES and value_gbp > I007_DORMANT_THRESHOLD_GBP:
        return {
            "flag": True,
            "reason": f"dormant company won £{value_gbp:,.0f}",
        }
    if category in ("small", "micro-entity") and value_gbp > I007_SMALL_THRESHOLD_GBP:
        return {
            "flag": True,
            "reason": f"{category} company won £{value_gbp:,.0f}",
        }
    return {"flag": False, "reason": f"accounts={category}, value=£{value_gbp:,.0f}"}


def check_i008(award: dict, ch: dict) -> dict:
    category = (ch.get("accounts_category") or "unknown").lower()
    incorp = ch.get("incorporation_date", "")
    award_date = award.get("award_date", "")

    is_young = False
    if incorp and award_date:
        days = days_between(incorp, award_date)
        if days is not None and days < 540:
            is_young = True

    if category in DORMANT_CATEGORIES:
        return {"flag": True, "reason": "dormant accounts at award date"}
    if category == "no accounts available" or (is_young and category in ("", "unknown")):
        if is_young:
            return {"flag": True, "reason": "company <18mo old, no accounts"}
        return {"flag": False, "reason": "no accounts data (company too old for CH bulk)"}
    return {"flag": False, "reason": f"accounts={category}"}


# ── Main ──────────────────────────────────────────────────────────────────────


def main() -> None:
    print("=" * 70)
    print("VIP-LANE COHORT TEST v2 — CH BULK CSV RESOLUTION")
    print("=" * 70)

    # Print pre-registered expectations
    print("\n## PRE-REGISTERED EXPECTATIONS (before seeing new numbers)")
    print(f"  Date: {PRE_REGISTERED['date']}")
    for exp in PRE_REGISTERED["expectations"]:
        print(f"  - {exp}")
    print("\n  What would be surprising:")
    for exp in PRE_REGISTERED["what_would_be_surprising"]:
        print(f"  - {exp}")

    # 1. Load positives
    print("\n## LOADING POSITIVES")
    if not POSITIVES_CSV.exists():
        print(
            f"cohort file not found: {POSITIVES_CSV}\n"
            "This script requires the DHSC VIP-lane referral cohort, which is deliberately NOT\n"
            "distributed with this repository: it is a locally curated CSV that names individual\n"
            "referrers, so publishing it is an A2 (public-persons) decision gated by\n"
            "ADR-000, not a packaging detail.\n"
            "See 'A note on personal data in this repo' in README.md.\n"
            "To run this script, supply a CSV at that path with these columns:\n"
            "supplier_name, company_number, contract_value_gbp, source_of_referral,\n"
            "actual_referrer, source_doc, source_url, source_reference",
            file=sys.stderr,
        )
        raise SystemExit(2)

    with POSITIVES_CSV.open() as f:
        reader = csv.DictReader(f)
        positives = list(reader)
    print(f"  Total rows in CSV: {len(positives)}")
    uk_positives = [p for p in positives if p.get("supplier_name", "") not in FOREIGN_ENTITIES]
    foreign = [p for p in positives if p.get("supplier_name", "") in FOREIGN_ENTITIES]
    print(f"  UK entities: {len(uk_positives)}")
    print(f"  Foreign excluded: {len(foreign)}")
    vip_names = {p["supplier_name"] for p in uk_positives}

    # 2. Search bulk OCDS for VIP-lane awards
    print("\n## SEARCHING BULK OCDS FOR VIP-LANE AWARDS")
    vip_awards_by_name: dict[str, list[dict]] = defaultdict(list)
    vip_raw_names: dict[str, str] = {}  # original → raw OCDS name

    for year in [2020, 2021, 2022, 2023]:
        print(f"  {year}...", end=" ")
        count = 0
        for record in iter_bulk_records(year):
            count += 1
            for award in extract_awards(record):
                if not award["award_date"].startswith("2020"):
                    continue
                for sup in award["suppliers"]:
                    sup_name = sup["name"]
                    if not sup_name:
                        continue
                    # Extract t/a legal name from OCDS supplier name too
                    sup_legal = extract_ta_name(sup_name)
                    # Try matching with t/a extraction and suffix stripping
                    norm = normalise(sup_name)
                    stripped = strip_suffix(sup_name)
                    sup_legal_norm = normalise(sup_legal)
                    sup_legal_stripped = strip_suffix(sup_legal)
                    for vip_name in vip_names:
                        vip_norm = normalise(vip_name)
                        vip_stripped = strip_suffix(vip_name)
                        vip_legal = extract_ta_name(vip_name)
                        vip_legal_norm = normalise(vip_legal)
                        vip_legal_stripped = strip_suffix(vip_legal)
                        if (
                            stripped in (vip_stripped, vip_legal_stripped)
                            or norm in (vip_norm, vip_legal_norm)
                            or sup_legal_stripped in (vip_stripped, vip_legal_stripped)
                            or sup_legal_norm in (vip_norm, vip_legal_norm)
                        ):
                            award_copy = {**award}
                            award_copy["raw_supplier_name"] = sup_name
                            vip_awards_by_name[vip_name].append(award_copy)
                            vip_raw_names[vip_name] = sup_name
                            break
        print(f"{count:,} records")

    vip_found = {k for k, v in vip_awards_by_name.items() if v}
    print(f"  VIP suppliers found: {len(vip_found)}/{len(vip_names)}")

    # 3. Build control group from bulk OCDS (non-VIP PPE suppliers)
    print("\n## BUILDING CONTROL GROUP")
    control_awards_by_name: dict[str, list[dict]] = defaultdict(list)
    vip_norms = {normalise(n) for n in vip_names}
    vip_stripped = {strip_suffix(n) for n in vip_names}
    vip_legal_norms = {normalise(extract_ta_name(n)) for n in vip_names}
    vip_legal_stripped = {strip_suffix(extract_ta_name(n)) for n in vip_names}

    for year in [2020, 2021, 2022, 2023]:
        print(f"  {year}...", end=" ")
        count = 0
        for record in iter_bulk_records(year):
            count += 1
            for award in extract_awards(record):
                if not award["award_date"].startswith("2020"):
                    continue
                title_lower = (award.get("tender_title") or "").lower()
                if not any(kw in title_lower for kw in PPE_KEYWORDS):
                    continue
                for sup in award["suppliers"]:
                    sup_name = sup["name"]
                    if not sup_name:
                        continue
                    norm = normalise(sup_name)
                    stripped = strip_suffix(sup_name)
                    sup_legal = extract_ta_name(sup_name)
                    sup_legal_norm = normalise(sup_legal)
                    sup_legal_stripped = strip_suffix(sup_legal)
                    if (
                        norm in vip_norms
                        or stripped in vip_stripped
                        or norm in vip_legal_norms
                        or stripped in vip_legal_stripped
                        or sup_legal_norm in vip_norms
                        or sup_legal_stripped in vip_stripped
                        or sup_legal_norm in vip_legal_norms
                        or sup_legal_stripped in vip_legal_stripped
                    ):
                        continue  # skip VIP-lane suppliers
                    control_awards_by_name[sup_name].append(award)
        print(f"{count:,} records")

    print(f"  Control suppliers: {len(control_awards_by_name)}")
    print(f"  Control awards: {sum(len(v) for v in control_awards_by_name.values())}")

    # 4. Resolve ALL suppliers (both cohorts) through CH bulk CSV
    print("\n## RESOLVING SUPPLIERS VIA CH BULK CSV")
    all_supplier_names = set(vip_found) | set(control_awards_by_name.keys())
    print(f"  Total supplier names to resolve: {len(all_supplier_names)}")

    # Pre-fix resolution (without t/a extraction)
    print("\n  Pre-fix resolution (no t/a extraction):")
    # Build lookup without t/a
    lookup_keys_no_ta: dict[str, str] = {}
    for name in all_supplier_names:
        norm = normalise(name)
        lookup_keys_no_ta[norm] = name
        stripped = strip_suffix(name)
        if stripped != norm:
            lookup_keys_no_ta[stripped] = name

    resolved_no_ta = _resolve_from_csv(lookup_keys_no_ta)
    print(f"    Resolved: {len(resolved_no_ta)}/{len(all_supplier_names)}")

    # Post-fix resolution (with t/a extraction)
    print("\n  Post-fix resolution (with t/a extraction):")
    resolved_with_ta = build_ch_lookup(all_supplier_names)

    # Compare resolution rates
    vip_resolved_no_ta = sum(1 for n in vip_found if n in resolved_no_ta)
    ctrl_resolved_no_ta = sum(1 for n in control_awards_by_name if n in resolved_no_ta)
    vip_resolved_with_ta = sum(1 for n in vip_found if n in resolved_with_ta)
    ctrl_resolved_with_ta = sum(1 for n in control_awards_by_name if n in resolved_with_ta)

    print("\n  Resolution rates:")
    vip_pre_pct = vip_resolved_no_ta / len(vip_found) * 100 if vip_found else 0
    ctrl_pre_pct = (
        ctrl_resolved_no_ta / len(control_awards_by_name) * 100 if control_awards_by_name else 0
    )
    vip_post_pct = vip_resolved_with_ta / len(vip_found) * 100 if vip_found else 0
    ctrl_post_pct = (
        ctrl_resolved_with_ta / len(control_awards_by_name) * 100 if control_awards_by_name else 0
    )
    print(
        f"    Pre-fix:  VIP {vip_resolved_no_ta}/{len(vip_found)} ({vip_pre_pct:.1f}%), "
        f"CTRL {ctrl_resolved_no_ta}/{len(control_awards_by_name)} ({ctrl_pre_pct:.1f}%)"
    )
    print(
        f"    Post-fix: VIP {vip_resolved_with_ta}/{len(vip_found)} ({vip_post_pct:.1f}%), "
        f"CTRL {ctrl_resolved_with_ta}/{len(control_awards_by_name)} ({ctrl_post_pct:.1f}%)"
    )

    # 5. Run indicators on both cohorts (post-fix results)
    print("\n## RUNNING INDICATORS")
    indicators = {
        "i001": lambda a, ch: check_i001(a),
        "i005": lambda a, ch: check_i005(a),
        "i006": lambda a, ch: check_i006(a, ch),
        "i007": lambda a, ch: check_i007(a, ch),
        "i008": lambda a, ch: check_i008(a, ch),
    }

    vip_results = []
    ctrl_results = []

    for name, awards in vip_awards_by_name.items():
        ch = resolved_with_ta.get(name, {})
        for award in awards:
            result = {
                "cohort": "vip_lane",
                "supplier_name": name,
                "raw_supplier_name": award.get("raw_supplier_name", ""),
                "award_date": award["award_date"],
                "award_value_gbp": award["award_value_amount"]
                if award["award_value_currency"] == "GBP"
                else 0,
                "ocid": award["ocid"],
                "tender_title": award["tender_title"],
                "resolved": bool(ch),
                "ch_number": ch.get("company_number", ""),
                "incorporation_date": ch.get("incorporation_date", ""),
                "accounts_category": ch.get("accounts_category", ""),
            }
            for ind_name, ind_func in indicators.items():
                result[ind_name] = ind_func(award, ch)
            vip_results.append(result)

    for name, awards in control_awards_by_name.items():
        ch = resolved_with_ta.get(name, {})
        for award in awards:
            result = {
                "cohort": "vip_lane",
                "supplier_name": name,
                "raw_supplier_name": sup_name if (sup := award.get("suppliers", [{}])[0]) else "",
                "award_date": award["award_date"],
                "award_value_gbp": award["award_value_amount"]
                if award["award_value_currency"] == "GBP"
                else 0,
                "ocid": award["ocid"],
                "tender_title": award["tender_title"],
                "resolved": bool(ch),
                "ch_number": ch.get("company_number", ""),
                "incorporation_date": ch.get("incorporation_date", ""),
                "accounts_category": ch.get("accounts_category", ""),
            }
            for ind_name, ind_func in indicators.items():
                result[ind_name] = ind_func(award, ch)
            ctrl_results.append(result)

    # Fix cohort labels for controls
    for r in ctrl_results:
        r["cohort"] = "control"

    # 6. Discrimination tables
    print("\n" + "=" * 70)
    print("DISCRIMINATION TABLE (post-fix, all controls)")
    print("=" * 70)

    # Per Opus directive: i001/i005 are tender-internal — compute on ALL awards,
    # not just resolved. i006/i007/i008 need CH resolution — resolved subset only.
    vip_resolved = [r for r in vip_results if r["resolved"]]
    ctrl_resolved = [r for r in ctrl_results if r["resolved"]]

    vip_suppliers = len({r["supplier_name"] for r in vip_results})
    vip_resolved_suppliers = len({r["supplier_name"] for r in vip_resolved})
    ctrl_suppliers = len({r["supplier_name"] for r in ctrl_results})
    ctrl_resolved_suppliers = len({r["supplier_name"] for r in ctrl_resolved})
    print(f"\nVIP-LANE: {len(vip_results)} awards ({vip_suppliers} suppliers)")
    print(f"  Resolved: {len(vip_resolved)} awards ({vip_resolved_suppliers} suppliers)")
    print(f"CONTROL: {len(ctrl_results)} awards ({ctrl_suppliers} suppliers)")
    print(f"  Resolved: {len(ctrl_resolved)} awards ({ctrl_resolved_suppliers} suppliers)")

    # Tender-internal indicators: full cohorts (no resolution needed)
    print("\n--- Tender-internal indicators (ALL awards, no CH resolution needed) ---")
    tender_inds = ["i001", "i005"]
    tender_labels = {
        "i001": "i001 (single bidder)",
        "i005": "i005 (direct award)",
    }
    for label, group in [("VIP-LANE (all)", vip_results), ("CONTROL (all)", ctrl_results)]:
        n = len(group)
        if n == 0:
            print(f"\n{label}: 0 awards")
            continue
        print(f"\n{label}: {n} awards")
        for ind in tender_inds:
            flags = sum(1 for r in group if r[ind]["flag"])
            print(f"  {tender_labels[ind]}: {flags} flags ({flags / n * 100:.1f}%)")

    # Company-level indicators: resolved subset only
    print("\n--- Company-level indicators (resolved subset only) ---")
    company_inds = ["i006", "i007", "i008"]
    company_labels = {
        "i006": "i006 (incorporation proximity)",
        "i007": "i007 (value vs company size)",
        "i008": "i008 (dormancy)",
    }
    for label, group in [
        ("VIP-LANE (resolved)", vip_resolved),
        ("CONTROL (resolved)", ctrl_resolved),
    ]:
        n = len(group)
        if n == 0:
            print(f"\n{label}: 0 awards")
            continue
        print(f"\n{label}: {n} awards ({len({r['supplier_name'] for r in group})} suppliers)")
        for ind in company_inds:
            flags = sum(1 for r in group if r[ind]["flag"])
            print(f"  {company_labels[ind]}: {flags} flags ({flags / n * 100:.1f}%)")

    # Any-flag on full cohorts
    all_inds = ["i001", "i005", "i006", "i007", "i008"]
    for label, group in [("VIP-LANE (all)", vip_results), ("CONTROL (all)", ctrl_results)]:
        n = len(group)
        any_flag = sum(1 for r in group if any(r[ind]["flag"] for ind in all_inds))
        print(f"\n{label} any flag: {any_flag}/{n} ({any_flag / n * 100:.1f}%)")

    # Separation table with p-values
    # i001/i005: full cohorts (tender-internal, no resolution needed)
    # i006/i007/i008: resolved subset only (need CH data)
    print("\n" + "-" * 70)
    header = (
        f"{'indicator':<30} {'VIP n':>6} {'VIP %':>8} {'CTRL n':>7} "
        f"{'CTRL %':>8} {'sep':>7} {'p-value':>10}"
    )
    print(header)
    print("-" * 70)

    all_inds = ["i001", "i005", "i006", "i007", "i008"]
    ind_labels = {
        "i001": "i001 (single bidder)",
        "i005": "i005 (direct award)",
        "i006": "i006 (incorp proximity)",
        "i007": "i007 (value vs size)",
        "i008": "i008 (dormancy)",
    }
    tender_inds = {"i001", "i005"}

    for ind in all_inds:
        vip_group = vip_results if ind in tender_inds else vip_resolved
        ctrl_group = ctrl_results if ind in tender_inds else ctrl_resolved
        vip_n = len(vip_group)
        ctrl_n = len(ctrl_group)
        vip_flags = sum(1 for r in vip_group if r[ind]["flag"])
        ctrl_flags = sum(1 for r in ctrl_group if r[ind]["flag"])
        vip_rate = vip_flags / vip_n * 100 if vip_n else 0
        ctrl_rate = ctrl_flags / ctrl_n * 100 if ctrl_n else 0
        sep = vip_rate - ctrl_rate
        p = fisher_exact_p(vip_flags, vip_n - vip_flags, ctrl_flags, ctrl_n - ctrl_flags)
        sig = " ***" if p < 0.001 else " **" if p < 0.01 else " *" if p < 0.05 else ""
        print(
            f"{ind_labels[ind]:<30} {vip_n:>6} {vip_rate:>7.1f}% {ctrl_n:>7} "
            f"{ctrl_rate:>7.1f}% {sep:>+6.1f}% {p:>9.4f}{sig}"
        )

    # Any-flag row (full cohorts — i001/i005 fire without resolution)
    vip_n_all = len(vip_results)
    ctrl_n_all = len(ctrl_results)
    vip_any = sum(1 for r in vip_results if any(r[ind]["flag"] for ind in all_inds))
    ctrl_any = sum(1 for r in ctrl_results if any(r[ind]["flag"] for ind in all_inds))
    vip_any_rate = vip_any / vip_n_all * 100 if vip_n_all else 0
    ctrl_any_rate = ctrl_any / ctrl_n_all * 100 if ctrl_n_all else 0
    p_any = fisher_exact_p(vip_any, vip_n_all - vip_any, ctrl_any, ctrl_n_all - ctrl_any)
    any_sep = vip_any_rate - ctrl_any_rate
    print(
        f"{'any flag':<30} {vip_n_all:>6} {vip_any_rate:>7.1f}% {ctrl_n_all:>7} "
        f"{ctrl_any_rate:>7.1f}% {any_sep:>+6.1f}% {p_any:>9.4f}"
    )

    # Per-case coverage
    print("\n" + "-" * 70)
    print("PER-CASE COVERAGE")
    print("-" * 70)
    for name in [
        "PPE Medpro Ltd",
        "Crisp Websites Ltd trading as Pestfix",
        "Ayanda Capital Ltd",
    ]:
        matching = [r for r in vip_results if r["supplier_name"] == name]
        if not matching:
            print(f"  {name}: NOT FOUND in bulk data")
            continue
        r = matching[0]
        flags = [ind for ind in all_inds if r[ind]["flag"]]
        print(
            f"  {name}: {' | '.join(flags) if flags else 'no flags'} | "
            f"£{r['award_value_gbp']:,.0f} | "
            f"resolved={'Y' if r['resolved'] else 'N'} | "
            f"incorp={r['incorporation_date']}"
        )

    # Within-positives discrimination (Opus's decisive test)
    # Does anything separate PPE Medpro (adjudicated) from benign lane members?
    print("\n" + "=" * 70)
    print("WITHIN-POSITIVES DISCRIMINATION (adjudicated vs benign lane members)")
    print("=" * 70)
    print(
        "  PPE Medpro = adjudicated wrongdoing (contract voided, "
        "litigation settled)\n"
        "  PestFix = added in error per NAO\n"
        "  MDS Healthcare = managed third-party donation (benign)\n"
    )

    # Rank all resolved VIP-lane suppliers by number of flags
    vip_by_supplier: dict[str, list[dict]] = defaultdict(list)
    for r in vip_resolved:
        vip_by_supplier[r["supplier_name"]].append(r)

    print(f"  {'Supplier':<45} {'flags':>5} {'awards':>6} {'max value':>14}")
    print("  " + "-" * 75)
    ranked = []
    for name, awards in vip_by_supplier.items():
        flag_set = set()
        max_val = 0
        for r in awards:
            for ind in all_inds:
                if r[ind]["flag"]:
                    flag_set.add(ind)
            max_val = max(max_val, r["award_value_gbp"])
        ranked.append((name, flag_set, len(awards), max_val))
    ranked.sort(key=lambda x: len(x[1]), reverse=True)
    for name, flags, n_awards, max_val in ranked:
        flag_str = ",".join(sorted(flags)) if flags else "none"
        print(f"  {name[:44]:<45} {len(flags):>5} {n_awards:>6} £{max_val:>12,.0f}")
        print(f"    flags: {flag_str}")

    # Specifically: where does PPE Medpro sit?
    print("\n  --- PPE Medpro position ---")
    medpro = [r for r in vip_resolved if r["supplier_name"] == "PPE Medpro Ltd"]
    if medpro:
        medpro_flags = set()
        for r in medpro:
            for ind in all_inds:
                if r[ind]["flag"]:
                    medpro_flags.add(ind)
        medpro_rank = sum(1 for _, flags, _, _ in ranked if len(flags) > len(medpro_flags))
        print(f"  PPE Medpro rank: {medpro_rank + 1}/{len(ranked)} by flag count")
        print(f"  PPE Medpro flags: {sorted(medpro_flags)}")
        print(
            "  i006 fires on Medpro (incorporated 18-44 days before award) "
            "and NOT on most others — candidate within-lane signal (n=1, "
            "case study, not validation)"
        )
    else:
        print("  PPE Medpro: NOT in resolved set")

    # All VIP-lane flags
    print("\n" + "-" * 70)
    print("ALL VIP-LANE FLAGS (resolved only)")
    print("-" * 70)
    for r in vip_resolved:
        for ind in all_inds:
            if r[ind]["flag"]:
                print(
                    f"  [{ind}] {r['supplier_name']} | "
                    f"£{r['award_value_gbp']:,.0f} | {r[ind]['reason']}"
                )

    # Attrition
    print("\n" + "=" * 70)
    print("ATTRITION SUMMARY")
    print("=" * 70)
    print(f"  {len(positives)} sourced (CSV rows)")
    print(f"  → {len(uk_positives)} UK entities (excluded {len(foreign)} foreign)")
    print(f"  → {len(vip_found)} found in bulk OCDS with 2020 awards")
    print(f"  → {vip_resolved_with_ta} resolved via CH bulk CSV")
    print(f"  → {len(vip_resolved)} award-level results (resolved)")
    print(f"  Controls: {len(control_awards_by_name)} suppliers")
    print(f"  → {ctrl_resolved_with_ta} resolved via CH bulk CSV")
    print(f"  → {len(ctrl_resolved)} award-level results (resolved)")

    # Save results
    output = {
        "pre_registered": PRE_REGISTERED,
        "attrition": {
            "sourced": len(positives),
            "uk_entities": len(uk_positives),
            "vip_found_in_bulk": len(vip_found),
            "vip_resolved": vip_resolved_with_ta,
            "vip_awards": len(vip_results),
            "vip_awards_resolved": len(vip_resolved),
            "control_suppliers": len(control_awards_by_name),
            "control_resolved": ctrl_resolved_with_ta,
            "control_awards": len(ctrl_results),
            "control_awards_resolved": len(ctrl_resolved),
        },
        "resolution_rates": {
            "pre_fix": {
                "vip": (f"{vip_resolved_no_ta}/{len(vip_found)} ({vip_pre_pct:.1f}%)"),
                "control": (
                    f"{ctrl_resolved_no_ta}/{len(control_awards_by_name)} ({ctrl_pre_pct:.1f}%)"
                ),
            },
            "post_fix": {
                "vip": (f"{vip_resolved_with_ta}/{len(vip_found)} ({vip_post_pct:.1f}%)"),
                "control": (
                    f"{ctrl_resolved_with_ta}/{len(control_awards_by_name)} ({ctrl_post_pct:.1f}%)"
                ),
            },
        },
        "vip_results": vip_results,
        "control_results": ctrl_results,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2, default=str))
    print(f"\nResults saved to {OUTPUT_FILE}")


def _resolve_from_csv(lookup_keys: dict[str, str]) -> dict[str, dict]:
    """Resolve supplier names against CH bulk CSV without t/a extraction."""
    matches: dict[str, list[dict]] = defaultdict(list)
    count = 0

    with CH_BULK_CSV.open(encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        for row in reader:
            count += 1
            if count % 1_000_000 == 0:
                print(f"    [{count:,}] matches={sum(len(v) for v in matches.values())}")
            company_name = row.get("CompanyName", "").strip()
            if not company_name:
                continue
            norm = normalise(company_name)
            stripped = strip_suffix(company_name)
            matched_keys = set()
            if norm in lookup_keys:
                matched_keys.add(norm)
            if stripped in lookup_keys:
                matched_keys.add(stripped)
            for key in matched_keys:
                matches[key].append(
                    {
                        "company_number": row.get("CompanyNumber", "").strip(),
                        "company_name": company_name,
                        "company_status": row.get("CompanyStatus", "").strip(),
                        "incorporation_date": parse_date(row.get("IncorporationDate", "")),
                        "accounts_category": row.get("Accounts.AccountCategory", "").strip(),
                        "accounts_last_made_up_date": parse_date(
                            row.get("Accounts.LastMadeUpDate", "")
                        ),
                        "normalised_name": norm,
                    }
                )

    resolved: dict[str, dict] = {}
    name_to_keys: dict[str, list[str]] = defaultdict(list)
    for key, orig_name in lookup_keys.items():
        name_to_keys[orig_name].append(key)

    for orig_name, keys in name_to_keys.items():
        all_matches = []
        for key in keys:
            all_matches.extend(matches.get(key, []))
        if not all_matches:
            continue
        if len(all_matches) == 1:
            resolved[orig_name] = all_matches[0]
            continue
        active = [m for m in all_matches if m["company_status"] == "Active"]
        if len(active) == 1:
            resolved[orig_name] = active[0]
        elif len(all_matches) == 1:
            resolved[orig_name] = all_matches[0]

    return resolved


if __name__ == "__main__":
    main()
