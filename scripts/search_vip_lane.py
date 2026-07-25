"""Search the Contracts Finder OCDS API for VIP-lane suppliers by name.

The VIP-lane contracts were often published with delay (2021-2022).
We search the OCDS API with wide date ranges and filter by supplier name.

Known VIP-lane suppliers (from NAO report + Good Law Project):
  - PPE Medpro Ltd
  - Crisp Websites Ltd t/a PestFix
  - Ayanda Capital Ltd
  - Uniserve (HK) Ltd / Uniserve Ltd
  - Worldlink Medical / Worldlink Resources
  - Supply Chain Excellence Ltd
  - Celframe Technology Ltd
  - VGC Health
"""

import json
import time
from pathlib import Path

import httpx

API_URL = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
PAGE_SIZE = 100

VIP_SUPPLIERS = [
    "ppe medpro",
    "medpro",
    "pestfix",
    "crisp websites",
    "ayanda",
    "uniserve",
    "worldlink",
    "supply chain excellence",
    "celframe",
    "vgc health",
    "mone",
]

OUTPUT_DIR = Path("experiments/snapshot_uk_covid_2020")


def fetch_page(since: str, cursor: str | None = None, until: str | None = None) -> dict:
    params: dict = {"limit": PAGE_SIZE, "publishedFrom": since}
    if until:
        params["publishedTo"] = until
    if cursor:
        params["cursor"] = cursor

    for attempt in range(8):
        resp = httpx.get(API_URL, params=params, timeout=30.0)
        if resp.status_code == 429:
            wait = 15 * (attempt + 1)
            print(f"  429 — waiting {wait}s...")
            time.sleep(wait)
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Rate limited 8 times — giving up")


def is_vip_supplier(release: dict) -> bool:
    """Check if any award supplier matches a VIP-lane name."""
    for award in release.get("awards", []):
        for sup in award.get("suppliers", []):
            name = (sup.get("name") or "").lower()
            for vip in VIP_SUPPLIERS:
                if vip in name:
                    return True
    # Also check buyer/title
    tender = release.get("tender", {})
    title = (tender.get("title") or "").lower()
    return any(vip in title for vip in VIP_SUPPLIERS)


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # Search 2020-01-01 to 2022-12-31 (VIP-lane contracts were published with delay)
    since = "2020-01-01"
    until = "2022-12-31"

    all_releases: list[dict] = []
    vip_releases: list[dict] = []
    cursor = None
    page_num = 0
    total_seen = 0

    print(f"Searching {since} to {until} for VIP-lane suppliers...")
    print(f"Looking for: {', '.join(VIP_SUPPLIERS)}")
    print()

    try:
        while True:
            page_num += 1
            data = fetch_page(since, cursor, until)
            releases = data.get("releases", [])

            if not releases:
                print(f"Page {page_num}: no releases — stopping")
                break

            oldest_date = releases[-1].get("date", "")
            if oldest_date and oldest_date < since:
                in_range = [r for r in releases if r.get("date", "") >= since]
                all_releases.extend(in_range)
                vip = [r for r in in_range if is_vip_supplier(r)]
                vip_releases.extend(vip)
                total_seen += len(in_range)
                print(f"Page {page_num}: {len(in_range)} in range — reached start date")
                break

            total_seen += len(releases)
            all_releases.extend(releases)
            vip = [r for r in releases if is_vip_supplier(r)]
            vip_releases.extend(vip)

            print(
                f"Page {page_num}: {len(releases)} releases "
                f"(total: {total_seen}, VIP: {len(vip_releases)})"
            )

            if vip:
                for r in vip:
                    for award in r.get("awards", []):
                        for sup in award.get("suppliers", []):
                            print(
                                f"  FOUND: {sup.get('name', '')} | "
                                f"ocid: {r.get('ocid', '')} | "
                                f"date: {r.get('date', '')}"
                            )

            # Checkpoint save every 20 pages
            if page_num % 20 == 0:
                _save(vip_releases, OUTPUT_DIR / "vip_lane_suppliers.json")
                print("  [checkpoint]")

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

            time.sleep(2.0)
    except Exception as e:
        print(f"ERROR: {e}")
        print("Saving partial results...")

    _save(vip_releases, OUTPUT_DIR / "vip_lane_suppliers.json")
    print()
    print("=== Summary ===")
    print(f"Total releases scanned: {total_seen}")
    print(f"VIP-lane releases found: {len(vip_releases)}")
    for r in vip_releases:
        for award in r.get("awards", []):
            for sup in award.get("suppliers", []):
                print(f"  {sup.get('name', '')} | {r.get('ocid', '')} | {r.get('date', '')}")


def _save(releases: list[dict], path: Path) -> None:
    artifacts = []
    for r in releases:
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
    path.write_text(json.dumps(artifacts, indent=2))
    print(f"  Saved {len(artifacts)} VIP-lane artifacts → {path}")


if __name__ == "__main__":
    main()
