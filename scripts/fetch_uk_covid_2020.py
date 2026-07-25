"""Fetch 2020 UK COVID-era contracts from Contracts Finder.

The Contracts Finder OCDS API supports date filtering via `publishedFrom`.
We fetch all contracts published 2020-03-01 to 2020-12-31, then filter
client-side for PPE/medical CPV codes.

CPV codes for PPE/medical:
  18100000-18130000 — workwear, protective clothing
  33100000-33190000 — medical equipment
  33600000          — pharmaceuticals
  35200000          — medical imaging
  39530000          — disinfectants
  42990000          — ventilators (rough)
  48810000          — clinical software (rough)

Usage:
    uv run python scripts/fetch_uk_covid_2020.py
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import httpx

API_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
PAGE_SIZE = 100

# PPE/medical CPV code prefixes (matching the first 2-3 digits)
PPE_CPV_PREFIXES = (
    "18",  # workwear, protective clothing
    "33",  # medical equipment, pharmaceuticals
    "35",  # medical imaging (subset)
    "39530",  # disinfectants
)

OUTPUT_DIR = Path("experiments/snapshot_uk_covid_2020")
OUTPUT_FILE = OUTPUT_DIR / "uk_contracts_finder.json"


def is_pp_medical(release: dict) -> bool:
    """Check if a release mentions PPE/medical supplies in CPV codes or title."""
    # Check CPV codes in tender.items
    tender = release.get("tender", {})
    for item in tender.get("items", []):
        classification = item.get("classification", {})
        scheme = classification.get("scheme", "")
        code = str(classification.get("id", ""))
        if scheme == "CPV" and any(code.startswith(p) for p in PPE_CPV_PREFIXES):
            return True
        # Also check additional classifications
        for add_cls in item.get("additionalClassifications", []):
            if add_cls.get("scheme") == "CPV":
                code = str(add_cls.get("id", ""))
                if any(code.startswith(p) for p in PPE_CPV_PREFIXES):
                    return True

    # Also check title/description for PPE keywords (broader)
    title = (tender.get("title") or "").lower()
    description = (tender.get("description") or "").lower()
    ppe_keywords = (
        "ppe",
        "personal protective",
        "surgical mask",
        "face mask",
        "n95",
        "ffp2",
        "ffp3",
        "ventilator",
        "sanitiser",
        "sanitizer",
        "disinfectant",
        "gown",
        "apron",
        "glove",
        "medical",
        "covid",
        "coronavirus",
        "pandemic",
    )
    return any(kw in title for kw in ppe_keywords) or any(kw in description for kw in ppe_keywords)


def fetch_page(since: str, cursor: str | None = None, until: str | None = None) -> dict:
    params: dict = {"limit": PAGE_SIZE, "publishedFrom": since}
    if until:
        params["publishedTo"] = until
    if cursor:
        params["cursor"] = cursor

    for attempt in range(5):
        resp = httpx.get(API_URL, params=params, timeout=30.0)
        if resp.status_code == 429:
            wait = 10 * (attempt + 1)
            print(f"  Rate limited (429) — waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Rate limited 5 times — giving up")


def _save(all_releases: list[dict], ppe_releases: list[dict], output_dir: Path) -> None:
    """Save releases as hex-encoded snapshot files."""
    artifacts = []
    for r in all_releases:
        raw = json.dumps(r, separators=(",", ":")).encode("utf-8")
        artifacts.append(
            {
                "source_url": (
                    "https://www.contractsfinder.service.gov.uk"
                    f"/Published/OCDS/Record/{r.get('ocid', '')}"
                ),
                "media_type": "application/json",
                "payload_hex": raw.hex(),
            }
        )

    (output_dir / "uk_contracts_finder.json").write_text(json.dumps(artifacts, indent=2))
    print(
        f"Total releases: {len(all_releases)}, "
        f"PPE/medical: {len(ppe_releases)} → "
        f"{output_dir / 'uk_contracts_finder.json'}"
    )

    ppe_artifacts = []
    for r in ppe_releases:
        raw = json.dumps(r, separators=(",", ":")).encode("utf-8")
        ppe_artifacts.append(
            {
                "source_url": (
                    "https://www.contractsfinder.service.gov.uk"
                    f"/Published/OCDS/Record/{r.get('ocid', '')}"
                ),
                "media_type": "application/json",
                "payload_hex": raw.hex(),
            }
        )

    (output_dir / "uk_covid_ppe.json").write_text(json.dumps(ppe_artifacts, indent=2))


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    since = "2020-03-01"
    until = "2020-12-31"
    all_releases: list[dict] = []
    ppe_releases: list[dict] = []
    cursor = None
    page_num = 0
    total_seen = 0

    print(f"Fetching UK Contracts Finder data {since} to {until}")
    print(f"API: {API_URL}")
    print(f"Page size: {PAGE_SIZE}")
    print()

    try:
        while True:
            page_num += 1
            data = fetch_page(since, cursor, until)
            releases = data.get("releases", [])

            if not releases:
                print(f"Page {page_num}: no releases — stopping")
                break

            # Stop if we've paginated past the start date
            oldest_date = releases[-1].get("date", "")
            if oldest_date and oldest_date < since:
                # Still include releases within range
                in_range = [r for r in releases if r.get("date", "") >= since]
                all_releases.extend(in_range)
                ppe_in_range = [r for r in in_range if is_pp_medical(r)]
                ppe_releases.extend(ppe_in_range)
                total_seen += len(in_range)
                print(
                    f"Page {page_num}: {len(in_range)} in range "
                    f"(total: {total_seen}, PPE/medical: {len(ppe_releases)}) "
                    f"— reached start date"
                )
                break

            total_seen += len(releases)

            for r in releases:
                all_releases.append(r)
                if is_pp_medical(r):
                    ppe_releases.append(r)

            print(
                f"Page {page_num}: {len(releases)} releases "
                f"(total: {total_seen}, PPE/medical: {len(ppe_releases)})"
            )

            # Save checkpoint every 10 pages
            if page_num % 10 == 0:
                _save(all_releases, ppe_releases, OUTPUT_DIR)
                print("  [checkpoint saved]")

            # Get next cursor
            next_page = data.get("next_page", {})
            cursor = next_page.get("cursor") if isinstance(next_page, dict) else None
            if not cursor:
                links = data.get("links", {})
                next_link = links.get("next") if isinstance(links, dict) else None
                if isinstance(next_link, dict):
                    cursor = next_link.get("cursor")
                elif isinstance(next_link, str):
                    from urllib.parse import parse_qs, urlparse

                    parsed = urlparse(next_link)
                    cursor = parse_qs(parsed.query).get("cursor", [None])[0]
            if not cursor:
                print("No next cursor — stopping")
                break

            # Rate limit
            time.sleep(2.0)
    except Exception as e:
        print(f"ERROR: {e}")
        print("Saving partial results...")

    _save(all_releases, ppe_releases, OUTPUT_DIR)

    # Report key suppliers
    print()
    print("=== Key PPE suppliers found ===")
    key_suppliers = ("ppe medpro", "pestfix", "crisp websites", "ayanda", "uniserve")
    for r in ppe_releases:
        for award in r.get("awards", []):
            for sup in award.get("suppliers", []):
                name = (sup.get("name") or "").lower()
                if any(k in name for k in key_suppliers):
                    print(f"  {sup.get('name', '')} | ocid: {r.get('ocid', '')}")

    # Also check all releases (PPE filter might miss some)
    print()
    print("=== Key suppliers in ALL releases ===")
    for r in all_releases:
        for award in r.get("awards", []):
            for sup in award.get("suppliers", []):
                name = (sup.get("name") or "").lower()
                if any(k in name for k in key_suppliers):
                    print(f"  {sup.get('name', '')} | ocid: {r.get('ocid', '')}")


if __name__ == "__main__":
    main()
