"""Comparative kill experiment runner (ADR-002 D3).

Fetches a sample from each of the three procurement APIs, ingests into Django/PostgreSQL,
runs all five indicators, and produces a flag export ready for blind journalist
review. The goal: 10+ reproducible flags → 3 journalists blind-rate →
≥1 says "I'd chase this" or kill.

Usage:
    uv run python scripts/kill_experiment.py [--sample-size N] [--output flags.json]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date

import django
import httpx

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")
django.setup()

from uncorrupt.connectors.base import RawArtifact
from uncorrupt.indicators.catalog.i001_single_bidder import SingleBidder
from uncorrupt.indicators.catalog.i002_short_bid_window import ShortBidWindow
from uncorrupt.indicators.catalog.i003_repeat_winner_share import RepeatWinnerShare
from uncorrupt.indicators.catalog.i004_price_vs_estimate import PriceVsEstimate
from uncorrupt.indicators.catalog.i005_direct_award_share import DirectAwardShare
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.register.loader import load_locale
from uncorrupt.staging import ingest_artifacts
from uncorrupt.staging.models import Tender

UA_API = "https://api.openprocurement.org/api/2.5"
UK_API = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
CO_API = "https://www.datos.gov.co/resource/p6dx-8zbt.json"


def fetch_ua(n: int = 50) -> list[RawArtifact]:
    """Fetch n recent tenders from ProZorro (descending=1 gives newest first)."""
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


def fetch_uk(n: int = 50) -> list[RawArtifact]:
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


def fetch_co(n: int = 50) -> list[RawArtifact]:
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


def _lookup_tender_meta(source_id: str, subject_ref: str) -> dict:
    """Look up tender metadata for a flag's subject_ref using Django ORM."""
    try:
        tender = Tender.objects.get(tender_id=subject_ref)
    except Tender.DoesNotExist:
        pass
    else:
        return {
            "title": tender.title,
            "value_amount_cents": tender.value_amount_cents,
            "currency": tender.currency,
            "buyer_name": tender.buyer_name,
            "procurement_method": tender.procurement_method,
        }

    if "→" in subject_ref:
        buyer = subject_ref.split("→")[0].strip()
        tender = Tender.objects.filter(buyer_name__icontains=buyer).first()
        if tender:
            return {
                "title": tender.title,
                "value_amount_cents": tender.value_amount_cents,
                "currency": tender.currency,
                "buyer_name": tender.buyer_name,
                "procurement_method": tender.procurement_method,
            }

    if ":" in subject_ref:
        tender_id = subject_ref.split(":")[0]
        try:
            tender = Tender.objects.get(tender_id=tender_id)
        except Tender.DoesNotExist:
            pass
        else:
            return {
                "title": tender.title,
                "value_amount_cents": tender.value_amount_cents,
                "currency": tender.currency,
                "buyer_name": tender.buyer_name,
                "procurement_method": tender.procurement_method,
            }

    return {}


def run_experiment(sample_size: int = 50) -> dict:
    """Run the full kill experiment and return results as a dict."""
    # Clear staging tables for a fresh run
    from uncorrupt.staging.models import Award, Bid

    Tender.objects.all().delete()
    Award.objects.all().delete()
    Bid.objects.all().delete()

    print(f"Fetching {sample_size} records from each source...")
    sources = [
        ("ua_prozorro", fetch_ua, "ua"),
        ("uk_contracts_finder", fetch_uk, "gb"),
        ("co_secop_ii", fetch_co, "co"),
    ]

    for source_id, fetcher, _locale in sources:
        print(f"  {source_id}...", end=" ", flush=True)
        try:
            artifacts = fetcher(sample_size)
            count = ingest_artifacts(source_id, artifacts)
            print(f"ingested {count}/{len(artifacts)}")
        except Exception as e:
            print(f"ERROR: {e}")

    source_by_locale = {"ua": "ua_prozorro", "gb": "uk_contracts_finder", "co": "co_secop_ii"}
    indicators = [
        SingleBidder(),
        ShortBidWindow(),
        RepeatWinnerShare(),
        PriceVsEstimate(),
        DirectAwardShare(),
    ]

    all_flags = []
    for locale_code, source_id in source_by_locale.items():
        locale = load_locale(locale_code)
        ctx = EvaluationContext(locale=locale)

        print(f"\nRunning indicators for {locale_code} ({source_id})...")
        for ind in indicators:
            if not ind.runs_in(locale_code):
                continue
            flags = list(ind.evaluate(ctx))
            if flags:
                print(f"  {ind.id}: {len(flags)} flags")
                for f in flags:
                    meta = _lookup_tender_meta(source_id, f.subject_ref)
                    all_flags.append(
                        {
                            "indicator_id": f.indicator_id,
                            "subject_ref": f.subject_ref,
                            "as_of": f.as_of.isoformat(),
                            "explanation": f.explanation,
                            "evidence": [
                                {
                                    "source_id": e.source_id,
                                    "source_url": e.source_url,
                                    "license": e.license,
                                    "jurisdiction": e.jurisdiction,
                                }
                                for e in f.evidence
                            ],
                            "stamp": {
                                "data_snapshot": f.stamp.data_snapshot,
                                "code_version": f.stamp.code_version,
                                "indicator_version": f.stamp.indicator_version,
                            },
                            "tender_title": meta.get("title"),
                            "tender_value_cents": meta.get("value_amount_cents"),
                            "tender_currency": meta.get("currency"),
                            "buyer_name": meta.get("buyer_name"),
                            "procurement_method": meta.get("procurement_method"),
                        }
                    )
            else:
                print(f"  {ind.id}: 0 flags")

    by_indicator: dict[str, int] = {}
    by_jurisdiction: dict[str, int] = {}
    for flag in all_flags:
        by_indicator[flag["indicator_id"]] = by_indicator.get(flag["indicator_id"], 0) + 1
        jur = flag["evidence"][0]["jurisdiction"] if flag["evidence"] else "?"
        by_jurisdiction[jur] = by_jurisdiction.get(jur, 0) + 1

    print(f"\n{'=' * 60}")
    print(f"KILL EXPERIMENT RESULTS — {date.today()}")
    print(f"{'=' * 60}")
    print(f"Total flags: {len(all_flags)}")
    print("\nBy indicator:")
    for k, v in sorted(by_indicator.items()):
        print(f"  {k}: {v}")
    print("\nBy jurisdiction:")
    for k, v in sorted(by_jurisdiction.items()):
        print(f"  {k}: {v}")

    if len(all_flags) >= 10:
        print(f"\n✅ PASS: {len(all_flags)} flags ≥ 10 threshold for blind review.")
    else:
        print(f"\n⚠️  WARNING: Only {len(all_flags)} flags < 10 threshold.")

    return {
        "experiment_date": date.today().isoformat(),
        "sample_size_per_source": sample_size,
        "total_flags": len(all_flags),
        "by_indicator": by_indicator,
        "by_jurisdiction": by_jurisdiction,
        "flags": all_flags,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the comparative kill experiment.")
    parser.add_argument("--sample-size", type=int, default=50, help="Records per source")
    parser.add_argument("--output", default="flags.json", help="Output file for flags")
    args = parser.parse_args()

    results = run_experiment(args.sample_size)

    with open(args.output, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nFlags exported to {args.output}")

    # Exit 0 if we got 10+ flags, 1 otherwise
    sys.exit(0 if results["total_flags"] >= 10 else 1)


if __name__ == "__main__":
    main()
