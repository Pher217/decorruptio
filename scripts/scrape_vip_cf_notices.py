"""Search Contracts Finder for VIP-lane suppliers and scrape notice pages.

1. POST Search API with quoted keyword → notice UUIDs
2. Scrape each notice detail page for award data (date, value, supplier)
3. Filter to 2020 awards

Output: experiments/vip_lane_cf_awards.json
"""

from __future__ import annotations

import html
import json
import re
import time
from pathlib import Path

import httpx

RESOLUTION_FILE = Path("experiments/vip_lane_resolution.json")
OUTPUT_FILE = Path("experiments/vip_lane_cf_awards.json")

SEARCH_API = "https://www.contractsfinder.service.gov.uk/Searches/Search"
NOTICE_URL = "https://www.contractsfinder.service.gov.uk/notice/{notice_id}"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
}


def load_resolved_suppliers() -> list[dict]:
    data = json.loads(RESOLUTION_FILE.read_text())
    return [r for r in data["results"] if r["resolved"]]


def search_notices(keyword: str, size: int = 100) -> tuple[int, list[dict]]:
    """Search CF by quoted keyword. Returns (hit_count, notice_items)."""
    for attempt in range(4):
        resp = httpx.post(
            SEARCH_API,
            json={"SearchCriteria": {"Keyword": f'"{keyword}"'}, "Size": size},
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
        if resp.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f"  Search 429 — waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            return 0, []
        break
    else:
        return 0, []
    data = resp.json()
    hits = data.get("hitCount", 0)
    notices = data.get("noticeList", [])
    items = [n.get("item", {}) for n in notices]
    return hits, items


def scrape_notice_award(notice_id: str) -> dict | None:
    """Scrape a Contracts Finder notice page for award data."""
    for attempt in range(4):
        resp = httpx.get(
            NOTICE_URL.format(notice_id=notice_id),
            timeout=20.0,
            follow_redirects=True,
            headers=HEADERS,
        )
        if resp.status_code == 429:
            wait = 30 * (attempt + 1)
            print(f"    429 — waiting {wait}s...")
            time.sleep(wait)
            continue
        if resp.status_code != 200:
            return None
        break
    else:
        return None

    text = resp.text
    result = {
        "notice_id": notice_id,
        "title": "",
        "description": "",
        "award_date": "",
        "contract_start": "",
        "contract_end": "",
        "total_value": "",
        "suppliers": [],
    }

    # Extract title
    m = re.search(r"<title>\s*([^<]+?)\s*-\s*Contracts Finder", text)
    if m:
        result["title"] = m.group(1).strip()

    # Extract description
    m = re.search(r"heading[^>]*>Description</h\d>.*?<p[^>]*>(.*?)</p>", text, re.S)
    if m:
        desc = re.sub(r"<[^>]+>", "", m.group(1)).strip()
        result["description"] = desc[:500]

    # Extract awarded date
    m = re.search(r"Awarded date</strong>\s*</h\d>\s*<p[^>]*>\s*(.*?)\s*</p>", text, re.S)
    if m:
        result["award_date"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    # Also try "Date awarded" variant
    if not result["award_date"]:
        m = re.search(
            r"Date awarded</strong>\s*</h\d>\s*<p[^>]*>\s*(.*?)\s*</p>",
            text,
            re.S,
        )
        if m:
            result["award_date"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    # Extract contract start/end dates
    m = re.search(
        r"Contract start date</strong>\s*</h\d>\s*<p[^>]*>\s*(.*?)\s*</p>",
        text,
        re.S,
    )
    if m:
        result["contract_start"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    m = re.search(
        r"Contract end date</strong>\s*</h\d>\s*<p[^>]*>\s*(.*?)\s*</p>",
        text,
        re.S,
    )
    if m:
        result["contract_end"] = re.sub(r"<[^>]+>", "", m.group(1)).strip()

    # Extract total value
    m = re.search(
        r"Total value of contract</strong>\s*</h\d>\s*<p[^>]*>\s*(.*?)\s*</p>",
        text,
        re.S,
    )
    if m:
        result["total_value"] = html.unescape(re.sub(r"<[^>]+>", "", m.group(1)).strip())

    # Extract supplier names — they're in <h4><strong>Supplier Name</strong></h4>
    # after the "Award information" section
    award_section = text[text.find("Award information") :] if "Award information" in text else ""
    if award_section:
        supplier_names = re.findall(
            r"<h4[^>]*>\s*<strong[^>]*>\s*([^<]+?)\s*</strong>\s*</h4>",
            award_section,
        )
        # Filter out field labels and non-supplier headings
        field_labels = {
            "Awarded date",
            "Contract start date",
            "Contract end date",
            "Total value of contract",
            "Contact name",
            "Address",
            "Telephone",
            "Email",
            "Website",
            "Reference",
            "Supplier is SME?",
            "Supplier is VCSE?",
            "Company name",
        }
        for name in supplier_names:
            cleaned = name.strip()
            if cleaned not in field_labels and not cleaned.startswith("Contact"):
                result["suppliers"].append(cleaned)

    return result


def parse_date_to_iso(date_str: str) -> str:
    """Convert '30 May 2020' to '2020-05-30'."""
    months = {
        "January": "01",
        "February": "02",
        "March": "03",
        "April": "04",
        "May": "05",
        "June": "06",
        "July": "07",
        "August": "08",
        "September": "09",
        "October": "10",
        "November": "11",
        "December": "12",
    }
    for month, num in months.items():
        if month in date_str:
            m = re.search(r"(\d+)\s+" + month + r"\s+(\d{4})", date_str)
            if m:
                return f"{m.group(2)}-{num}-{int(m.group(1)):02d}"
    return ""


def parse_value(value_str: str) -> float:
    """Convert '£80,850,000' or '&pound;80,850,000' to 80850000.0."""
    decoded = html.unescape(value_str)
    cleaned = re.sub(r"[£,\s]", "", decoded)
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def main() -> None:
    resolved = load_resolved_suppliers()
    print(f"Resolved VIP-lane suppliers: {len(resolved)}")
    print()

    all_results = []

    for i, supplier in enumerate(resolved, 1):
        name = supplier["supplier_name"]
        search_name = name
        if " trading as " in search_name.lower():
            search_name = search_name.split(" trading as ")[0]
        if " t/a " in search_name.lower():
            search_name = search_name.split(" t/a ")[0]

        print(f'[{i}/{len(resolved)}] Searching: "{search_name}"')

        hits, notices = search_notices(search_name)
        print(f"  {hits} hits, {len(notices)} notices to scrape")

        # If 0 hits, try without Ltd/Limited suffix
        if not notices:
            stripped = search_name
            for sfx in [" Ltd", " Limited", " LTD", " LIMITED"]:
                if stripped.endswith(sfx):
                    stripped = stripped[: -len(sfx)]
                    break
            if stripped != search_name:
                hits, notices = search_notices(stripped)
                print(f'  Retry without suffix: "{stripped}" → {hits} hits')

        if not notices:
            all_results.append(
                {
                    "supplier_name": name,
                    "search_keyword": search_name,
                    "hits": 0,
                    "awards_2020": [],
                    "awards_other": [],
                }
            )
            continue

        awards = []
        for notice in notices:
            notice_id = notice.get("id", "")
            if not notice_id:
                continue

            award = scrape_notice_award(notice_id)
            if award and award["suppliers"]:
                award["published_date"] = notice.get("publishedDate", "")
                award["award_date_iso"] = parse_date_to_iso(award["award_date"])
                award["total_value_numeric"] = parse_value(award["total_value"])
                awards.append(award)

            time.sleep(0.5)  # Rate limit courtesy

        # Filter to 2020 awards
        awards_2020 = [a for a in awards if a["award_date_iso"].startswith("2020")]
        awards_other = [a for a in awards if not a["award_date_iso"].startswith("2020")]

        print(
            f"  → {len(awards)} awards with suppliers "
            f"({len(awards_2020)} in 2020, {len(awards_other)} other)"
        )
        for a in awards_2020[:5]:
            print(
                f"    {', '.join(a['suppliers'])} | {a['award_date']} | "
                f"{a['total_value']} | {a['title']}"
            )

        all_results.append(
            {
                "supplier_name": name,
                "search_keyword": search_name,
                "hits": hits,
                "awards_2020": awards_2020,
                "awards_other": awards_other,
            }
        )

        time.sleep(1)

    # Summary
    print()
    print("=" * 60)
    print("CONTRACTS FINDER NOTICE SCRAPE — SUMMARY")
    print("=" * 60)

    found = [r for r in all_results if r["awards_2020"]]
    not_found = [r for r in all_results if not r["awards_2020"]]

    total_2020 = sum(len(r["awards_2020"]) for r in all_results)
    total_value_2020 = sum(a["total_value_numeric"] for r in all_results for a in r["awards_2020"])

    print(f"  VIP suppliers searched:         {len(resolved)}")
    print(f"  Found with 2020 awards:         {len(found)}")
    print(f"  Total 2020 awards:              {total_2020}")
    print(f"  Total 2020 value:               £{total_value_2020:,.0f}")
    print()

    print("VIP suppliers WITH 2020 contract awards:")
    for r in found:
        print(f"  ✓ {r['supplier_name']} ({len(r['awards_2020'])} awards)")
        for a in r["awards_2020"]:
            print(
                f"    {', '.join(a['suppliers'])} | {a['award_date']} | "
                f"{a['total_value']} | {a['title']}"
            )

    print()
    print(f"VIP suppliers WITHOUT 2020 awards: {len(not_found)}")
    for r in not_found:
        other = len(r.get("awards_other", []))
        print(f"  ✗ {r['supplier_name']} ({r['hits']} hits, {other} non-2020 awards)")

    OUTPUT_FILE.write_text(json.dumps(all_results, indent=2))
    print(f"\nResults saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
