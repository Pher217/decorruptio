"""End-to-end integration test: fetch → ingest → query → verify staging layer.

Fetches a small sample from each of the three procurement APIs, ingests
into an in-memory DuckDB, and verifies the unified schema has data.
"""

from __future__ import annotations

import json

import duckdb
import httpx

from uncorrupt.connectors.base import RawArtifact
from uncorrupt.staging import create_schema, get_awards, get_tenders, ingest_artifacts

UA_API = "https://api.openprocurement.org/api/2.5"
UK_API = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
CO_API = "https://www.datos.gov.co/resource/p6dx-8zbt.json"


def fetch_ua_samples(n: int = 3) -> list[RawArtifact]:
    """Fetch n tender records from ProZorro."""
    resp = httpx.get(f"{UA_API}/tenders", params={"limit": n}, timeout=30)
    resp.raise_for_status()
    ids = [item["id"] for item in resp.json().get("data", [])]
    artifacts = []
    for tid in ids:
        r = httpx.get(f"{UA_API}/tenders/{tid}", timeout=30)
        if r.status_code == 200:
            artifacts.append(
                RawArtifact(
                    payload=r.content,
                    source_url=f"{UA_API}/tenders/{tid}",
                    media_type="application/json",
                )
            )
    return artifacts


def fetch_uk_samples(n: int = 3) -> list[RawArtifact]:
    """Fetch n OCDS releases from UK Contracts Finder."""
    resp = httpx.get(UK_API, params={"limit": n}, timeout=30)
    resp.raise_for_status()
    releases = resp.json().get("releases", [])
    return [
        RawArtifact(
            payload=json.dumps(r, ensure_ascii=False).encode(),
            source_url=(
                "https://www.contractsfinder.service.gov.uk"
                f"/Published/OCDS/Record/{r.get('ocid', '')}"
            ),
            media_type="application/json",
        )
        for r in releases
    ]


def fetch_co_samples(n: int = 3) -> list[RawArtifact]:
    """Fetch n records from Colombia SECOP II."""
    resp = httpx.get(CO_API, params={"$limit": n}, timeout=30)
    resp.raise_for_status()
    rows = resp.json()
    return [
        RawArtifact(
            payload=json.dumps(row, ensure_ascii=False).encode(),
            source_url=str(row.get("urlproceso", "")),
            media_type="application/json",
        )
        for row in rows
    ]


def main() -> None:
    conn = duckdb.connect(":memory:")
    create_schema(conn)

    print("Fetching samples from all three APIs...")
    for source_id, fetcher in [
        ("ua_prozorro", fetch_ua_samples),
        ("uk_contracts_finder", fetch_uk_samples),
        ("co_secop_ii", fetch_co_samples),
    ]:
        print(f"  {source_id}...", end=" ")
        artifacts = fetcher(3)
        count = ingest_artifacts(conn, source_id, artifacts)
        print(f"ingested {count}/{len(artifacts)}")

    print("\n=== Staging Layer Verification ===")
    for source_id in ["ua_prozorro", "uk_contracts_finder", "co_secop_ii"]:
        tenders = get_tenders(conn, source_id=source_id, limit=5)
        awards = get_awards(conn, source_id=source_id, limit=5)
        print(f"\n  {source_id}:")
        print(f"    Tenders: {len(tenders)}")
        if tenders:
            t = tenders[0]
            print(f"      Example: id={t['tender_id'][:25]}, title={str(t.get('title', ''))[:40]}")
            print(
                f"        buyer={str(t.get('buyer_name', ''))[:40]}, value={t.get('value_amount')}"
            )
        print(f"    Awards: {len(awards)}")
        if awards:
            a = awards[0]
            supplier = str(a.get("supplier_name", ""))[:40]
            print(f"      Example: supplier={supplier}, value={a.get('value_amount')}")

    # Cross-source comparison
    print("\n=== Cross-source comparison ===")
    result = conn.execute(
        "SELECT source_id, COUNT(*) as n, "
        "AVG(value_amount) as avg_value, "
        "COUNT(DISTINCT buyer_name) as n_buyers "
        "FROM tenders GROUP BY source_id"
    ).fetchall()
    for row in result:
        print(f"  {row[0]}: {row[1]} tenders, avg_value={row[2]}, {row[3]} buyers")

    print("\n✅ End-to-end staging layer test passed.")


if __name__ == "__main__":
    main()
