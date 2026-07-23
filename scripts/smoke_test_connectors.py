"""Live smoke test for all three procurement connectors.

Fetches a small sample from each API to verify connectivity and data structure.
Does NOT paginate through full history — just grabs the first page.
"""

from __future__ import annotations

import httpx

API_BASE_UA = "https://api.openprocurement.org/api/2.5"
API_BASE_UK = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
API_BASE_CO = "https://www.datos.gov.co/resource/p6dx-8zbt.json"


def test_ua_prozorro() -> None:
    """Fetch 2 tender IDs from ProZorro, then fetch the first one."""
    resp = httpx.get(f"{API_BASE_UA}/tenders", params={"limit": 2}, timeout=30)
    assert resp.status_code == 200
    data = resp.json()
    tender_ids = [item["id"] for item in data.get("data", [])]
    assert len(tender_ids) >= 1

    tender_id = tender_ids[0]
    resp2 = httpx.get(f"{API_BASE_UA}/tenders/{tender_id}", timeout=30)
    assert resp2.status_code == 200
    tender = resp2.json().get("data", {})
    assert "id" in tender
    print(f"  UA: id={tender['id'][:20]}, title={str(tender.get('title', ''))[:50]}")


def test_uk_contracts_finder() -> None:
    """Fetch 1 OCDS release from UK Contracts Finder."""
    resp = httpx.get(API_BASE_UK, params={"limit": 1}, timeout=30)
    assert resp.status_code == 200
    data = resp.json()
    releases = data.get("releases", [])
    assert len(releases) >= 1
    r = releases[0]
    title = str(r.get("tender", {}).get("title", ""))[:50]
    print(f"  UK: ocid={r.get('ocid', '?')[:30]}, title={title}")


def test_co_secop_ii() -> None:
    """Fetch 1 record from Colombia SECOP II."""
    resp = httpx.get(API_BASE_CO, params={"$limit": 1}, timeout=30)
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) >= 1
    row = rows[0]
    entidad = str(row.get("entidad", ""))[:50]
    print(f"  CO: id={row.get('id_del_proceso', '?')[:25]}, entidad={entidad}")


if __name__ == "__main__":
    print("=== Ukraine ProZorro ===")
    test_ua_prozorro()
    print("\n=== UK Contracts Finder ===")
    test_uk_contracts_finder()
    print("\n=== Colombia SECOP II ===")
    test_co_secop_ii()
    print("\n✅ All three APIs are live and returning data.")
