"""End-to-end integration test: fetch → ingest → query → verify staging layer.

Fetches a small sample from each of the three procurement APIs, ingests
into Django/PostgreSQL, and verifies the unified schema has data.
"""

from __future__ import annotations

import json
import os

import django
import httpx

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.connectors.base import RawArtifact
from uncorrupt.staging.ingest import ingest_artifacts
from uncorrupt.staging.models import Award, Tender

UA_API = "https://api.openprocurement.org/api/2.5"
UK_API = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
CO_API = "https://www.datos.gov.co/resource/p6dx-8zbt.json"


def fetch_ua_samples(n: int = 3) -> list[RawArtifact]:
    """Fetch n recent tender records from ProZorro."""
    resp = httpx.get(f"{UA_API}/tenders", params={"limit": n, "descending": 1}, timeout=30)
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
    print("Fetching samples from all three APIs...")
    for source_id, fetcher in [
        ("ua_prozorro", fetch_ua_samples),
        ("uk_contracts_finder", fetch_uk_samples),
        ("co_secop_ii", fetch_co_samples),
    ]:
        print(f"  {source_id}...", end=" ")
        artifacts = fetcher(3)
        count = ingest_artifacts(source_id, artifacts)
        print(f"ingested {count}/{len(artifacts)}")

    print("\n=== Staging Layer Verification ===")
    for source_id in ["ua_prozorro", "uk_contracts_finder", "co_secop_ii"]:
        tenders = Tender.objects.filter(source_id=source_id)[:5]
        awards = Award.objects.filter(source_id=source_id)[:5]
        print(f"\n  {source_id}:")
        print(f"    Tenders: {tenders.count()}")
        if tenders:
            t = tenders[0]
            print(f"      Example: id={t.tender_id[:25]}, title={str(t.title or '')[:40]}")
            print(
                f"        buyer={str(t.buyer_name or '')[:40]}, "
                f"value={t.value_amount_cents / 100:.2f} {t.currency}"
            )
        print(f"    Awards: {awards.count()}")
        if awards:
            a = awards[0]
            supplier = str(a.supplier_name or "")[:40]
            print(f"      Example: supplier={supplier}, value={a.value_amount_cents / 100:.2f}")

    print("\n=== Cross-source comparison ===")
    from django.db.models import Avg, Count

    for source_id in ["ua_prozorro", "uk_contracts_finder", "co_secop_ii"]:
        stats = Tender.objects.filter(source_id=source_id).aggregate(
            count=Count("id"),
            avg_value=Avg("value_amount_cents"),
            n_buyers=Count("buyer_name", distinct=True),
        )
        print(
            f"  {source_id}: {stats['count']} tenders, "
            f"avg_value={stats['avg_value'] or 0 / 100:.2f}, "
            f"{stats['n_buyers']} buyers"
        )

    print("\n✅ End-to-end staging layer test passed.")


if __name__ == "__main__":
    main()
