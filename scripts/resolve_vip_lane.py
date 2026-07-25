"""Resolve VIP-lane suppliers against Companies House via the public search.

Uses find-and-update.company-information.service.gov.uk (no API key needed).

Steps:
1. Read VIP-lane positives CSV (52 suppliers)
2. Exclude 5 foreign entities (no CH record expected)
3. For each UK supplier, search CH by name, fetch company details
4. Report attrition: 52 sourced → 47 UK → N resolved
5. Save to experiments/vip_lane_resolution.json
"""

from __future__ import annotations

import csv
import html
import json
import re
import time
from pathlib import Path

import httpx

POSITIVES_CSV = Path(".consult/vip_lane_positives.csv")
OUTPUT_DIR = Path("experiments")
OUTPUT_FILE = OUTPUT_DIR / "vip_lane_resolution.json"

FOREIGN_ENTITIES = [
    "JD.COM",
    "Headwind Industrial (China) Ltd",
    "Liaoning Zhongquiao Overseas Exchange Co Ltd",
    "New Asia Logistic Service PTE Ltd",
    "Wuhan Xiaoyaoyao Pharmaceutical",
]

CH_SEARCH = "https://find-and-update.company-information.service.gov.uk/search/companies"
CH_COMPANY = "https://find-and-update.company-information.service.gov.uk/company/{num}"

SUFFIXES = ["LTD", "LIMITED", "PLC", "LLP", "LP", "CIC"]


def normalise(name: str) -> str:
    return " ".join(name.upper().split())


def strip_suffix(name: str) -> str:
    norm = normalise(name)
    for s in SUFFIXES:
        if norm.endswith(" " + s):
            return norm[: -(len(s) + 1)]
    return norm


def extract_legal_name(name: str) -> str:
    """Extract legal entity from 'X trading as Y' or 'X t/a Y'."""
    lower = name.lower()
    if " trading as " in lower:
        return name.split(" trading as ")[0].strip()
    if " t/a " in lower:
        return name.split(" t/a ")[0].strip()
    return name


def search_ch(name: str) -> list[dict]:
    """Search Companies House public search. Returns [{company_number, title}, ...]."""
    search_name = extract_legal_name(name)
    resp = httpx.get(CH_SEARCH, params={"q": search_name}, timeout=15.0, follow_redirects=True)
    if resp.status_code != 200:
        return []
    pattern = re.compile(r'<a[^>]*href="/company/([A-Z0-9]+)"[^>]*>([^<]+)</a>', re.S)
    results = []
    for m in pattern.finditer(resp.text):
        results.append({"company_number": m.group(1), "title": html.unescape(m.group(2).strip())})
    return results[:10]


def fetch_company_details(company_number: str) -> dict:
    """Fetch company details from the public company page."""
    resp = httpx.get(CH_COMPANY.format(num=company_number), timeout=15.0, follow_redirects=True)
    if resp.status_code != 200:
        return {}
    text = resp.text

    # Status
    status = ""
    m = re.search(r'class="govuk-tag\s+govuk-tag--([a-z]+)"[^>]*>\s*(\w+)', text)
    if m:
        status = m.group(2).lower()

    # Incorporation date
    incorp = ""
    m = re.search(r"Incorporated on</dt>\s*<dd[^>]*>\s*(\d{1,2}\s+\w+\s+\d{4})", text, re.S)
    if m:
        incorp = m.group(1)
    else:
        m = re.search(r"(\d{1,2}\s+\w+\s+\d{4})\s*</dd>\s*</dl>\s*<p\s+class", text)
        if m:
            incorp = m.group(1)

    # Accounts category
    accounts = ""
    m = re.search(r"Accounts.next.due</dt>.*?<dd[^>]*>\s*(.*?)\s*</dd>", text, re.S)
    if m:
        accounts = m.group(1).strip()
    else:
        m = re.search(r"Accounts[\s\S]*?category.*?<strong[^>]*>([^<]+)</strong>", text, re.I)
        if m:
            accounts = m.group(1).strip()

    # Company name from page
    name = ""
    m = re.search(r"<title>\s*([^<]+?)\s*-\s*Find", text)
    if m:
        name = m.group(1).strip()

    return {
        "company_status": status,
        "incorporation_date": incorp,
        "accounts_category": accounts,
        "company_name_from_page": name,
    }


def judge_match(vip_name: str, ch_title: str) -> str:
    """Judge the quality of a CH search result match."""
    vip_clean = extract_legal_name(vip_name)
    vip_norm = normalise(vip_clean)
    ch_norm = normalise(ch_title)

    if vip_norm == ch_norm:
        return "exact"
    if strip_suffix(vip_clean) == strip_suffix(ch_title):
        return "exact_suffix_variant"
    vip_core = strip_suffix(vip_clean)
    ch_core = strip_suffix(ch_title)
    if vip_core in ch_core or ch_core in vip_core:
        return "close_substring"
    return "no_match"


def resolve_supplier(name: str) -> dict:
    """Resolve a single supplier name against Companies House."""
    results = search_ch(name)
    if not results:
        return {
            "supplier_name": name,
            "resolved": False,
            "match_quality": "none",
            "company_number": None,
            "company_name": None,
            "company_status": None,
            "accounts_category": None,
            "incorporation_date": None,
            "ch_results_count": 0,
        }

    best_match = None
    best_quality = "no_match"
    for item in results:
        quality = judge_match(name, item["title"])
        if quality in ("exact", "exact_suffix_variant"):
            best_match = item
            best_quality = quality
            break
        if quality == "close_substring" and best_quality == "no_match":
            best_match = item
            best_quality = "close_substring"

    if not best_match:
        best_match = results[0]
        best_quality = "ambiguous"

    details = fetch_company_details(best_match["company_number"])

    return {
        "supplier_name": name,
        "resolved": best_quality in ("exact", "exact_suffix_variant"),
        "match_quality": best_quality,
        "company_number": best_match["company_number"],
        "company_name": details.get("company_name_from_page") or best_match["title"],
        "company_status": details.get("company_status", ""),
        "accounts_category": details.get("accounts_category", ""),
        "incorporation_date": details.get("incorporation_date", ""),
        "ch_results_count": len(results),
    }


def main() -> None:
    with open(POSITIVES_CSV) as f:
        all_suppliers = list(csv.DictReader(f))

    print(f"VIP-lane suppliers in CSV: {len(all_suppliers)}")

    uk_suppliers = [s for s in all_suppliers if s["supplier_name"] not in FOREIGN_ENTITIES]
    foreign_excluded = [s for s in all_suppliers if s["supplier_name"] in FOREIGN_ENTITIES]
    print(f"Foreign entities excluded: {len(foreign_excluded)}")
    for fe in foreign_excluded:
        print(f"  {fe['supplier_name']}")
    print(f"UK-resolvable candidates: {len(uk_suppliers)}")
    print()

    results = []
    resolved_count = 0
    for i, supplier in enumerate(uk_suppliers, 1):
        name = supplier["supplier_name"]
        print(f"[{i}/{len(uk_suppliers)}] Resolving: {name}")

        result = resolve_supplier(name)
        result["source_of_referral"] = supplier.get("source_of_referral", "")
        result["actual_referrer"] = supplier.get("actual_referrer", "")
        result["source_doc"] = supplier.get("source_doc", "")
        result["source_url"] = supplier.get("source_url", "")
        result["source_reference"] = supplier.get("source_reference", "")

        print(
            f"  → {result['match_quality']} | {result['company_number']} | "
            f"{result['company_name']} | {result['company_status']}"
        )
        if result["resolved"]:
            resolved_count += 1

        results.append(result)
        time.sleep(0.5)

    print()
    print("=" * 60)
    print("ATTRITION REPORT")
    print("=" * 60)
    print(f"  Sourced VIP-lane suppliers:     {len(all_suppliers)}")
    print(f"  Foreign entities excluded:     {len(foreign_excluded)}")
    print(f"  UK-resolvable candidates:      {len(uk_suppliers)}")
    print(f"  Resolved (exact/suffix):       {resolved_count}")
    print(f"  Not resolved:                  {len(uk_suppliers) - resolved_count}")
    print()

    quality_counts: dict[str, int] = {}
    for r in results:
        q = r["match_quality"]
        quality_counts[q] = quality_counts.get(q, 0) + 1
    print("Match quality breakdown:")
    for q, c in sorted(quality_counts.items()):
        print(f"  {q}: {c}")

    print()
    print("Unresolved suppliers:")
    for r in results:
        if not r["resolved"]:
            print(
                f"  {r['supplier_name']} → {r['match_quality']} "
                f"({r['ch_results_count']} CH results)"
            )

    OUTPUT_DIR.mkdir(exist_ok=True)
    output = {
        "total_sourced": len(all_suppliers),
        "foreign_excluded": len(foreign_excluded),
        "uk_candidates": len(uk_suppliers),
        "resolved": resolved_count,
        "not_resolved": len(uk_suppliers) - resolved_count,
        "results": results,
    }
    OUTPUT_FILE.write_text(json.dumps(output, indent=2))
    print(f"\nResults saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
