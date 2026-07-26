"""Comparative kill experiment runner (ADR-002 D3).

Fetches a sample from each of the three procurement APIs, ingests into Django/PostgreSQL,
runs all five indicators, and produces a flag export ready for blind journalist
review. The goal: up to 10 independently credible flags → 3 journalists blind-rate →
≥1 says "I'd chase this" or kill.

FROZEN SNAPSHOT: On first run, raw artifacts are saved to experiments/snapshot_YYYY-MM-DD/.
On subsequent runs with --snapshot-dir, artifacts are loaded from disk, making
inputs frozen and reproducible for journalist review. Note: as_of / retrieved_at /
data_snapshot timestamps use today()/now(), so re-runs differ in those fields —
the inputs are frozen, not the output timestamps.

Usage:
    uv run python scripts/kill_experiment.py [--sample-size N] [--output experiments/flags_raw.json]
    uv run python scripts/kill_experiment.py --snapshot-dir experiments/snapshot_2026-07-23
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any

import django
import httpx

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

# Base-rate suppression threshold — shared with curate_flags.py (single source of truth)
from scripts.curate_flags import BASE_RATE_THRESHOLD

from uncorrupt.connectors.base import RawArtifact
from uncorrupt.indicators.catalog.i001_single_bidder import SingleBidder
from uncorrupt.indicators.catalog.i002_short_bid_window import ShortBidWindow
from uncorrupt.indicators.catalog.i003_repeat_winner_share import RepeatWinnerShare
from uncorrupt.indicators.catalog.i004_price_vs_estimate import PriceVsEstimate
from uncorrupt.indicators.catalog.i005_direct_award_share import DirectAwardShare
from uncorrupt.indicators.catalog.i006_incorporation_proximity import IncorporationProximity
from uncorrupt.indicators.catalog.i007_value_vs_company_size import ValueVsCompanySize
from uncorrupt.indicators.catalog.i008_dormancy_delinquency import DormancyDelinquency
from uncorrupt.indicators.context import EvaluationContext
from uncorrupt.register.loader import load_locale
from uncorrupt.staging.ingest import ingest_artifacts
from uncorrupt.staging.models import Tender

UA_API = "https://api.openprocurement.org/api/2.5"
UK_API = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
CO_API = "https://www.datos.gov.co/resource/p6dx-8zbt.json"


def fetch_ua(n: int = 200) -> list[RawArtifact]:
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


def fetch_uk(n: int = 200) -> list[RawArtifact]:
    """Fetch n OCDS releases from UK Contracts Finder."""
    artifacts: list[RawArtifact] = []
    cursor = ""
    while len(artifacts) < n:
        params: dict[str, str | int] = {"limit": min(100, n - len(artifacts))}
        if cursor:
            params["cursor"] = cursor
        resp = httpx.get(UK_API, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        releases = data.get("releases", [])
        if not releases:
            break
        for r in releases:
            artifacts.append(
                RawArtifact(
                    payload=json.dumps(r, ensure_ascii=False).encode(),
                    source_url=(
                        "https://www.contractsfinder.service.gov.uk"
                        f"/Published/OCDS/Record/{r.get('ocid', '')}"
                    ),
                    media_type="application/json",
                )
            )
        cursor = data.get("next", {}).get("uri", "")
        if not cursor:
            break
    return artifacts[:n]


def fetch_co(n: int = 200) -> list[RawArtifact]:
    """Fetch n adjudicated records from Colombia SECOP II (most recent first)."""
    artifacts: list[RawArtifact] = []
    offset = 0
    page_size = 1000
    while len(artifacts) < n:
        params: dict[str, str | int] = {
            "$limit": min(page_size, n - len(artifacts)),
            "$offset": offset,
            "$where": "adjudicado='Si'",
            "$order": "fecha_de_ultima_publicaci DESC",
        }
        resp = httpx.get(CO_API, params=params, timeout=30)
        resp.raise_for_status()
        rows = resp.json()
        if not rows:
            break
        for row in rows:
            artifacts.append(
                RawArtifact(
                    payload=json.dumps(row, ensure_ascii=False).encode(),
                    source_url=str(
                        (row.get("urlproceso") or {}).get("url", "")
                        if isinstance(row.get("urlproceso"), dict)
                        else row.get("urlproceso", "")
                    ),
                    media_type="application/json",
                )
            )
        offset += len(rows)
    return artifacts[:n]


def _save_snapshot(snapshot_dir: Path, source_id: str, artifacts: list[RawArtifact]) -> None:
    """Save raw artifacts to disk for frozen-snapshot reproducibility."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for a in artifacts:
        out.append(
            {
                "source_url": a.source_url,
                "media_type": a.media_type,
                "payload_hex": a.payload.hex(),
            }
        )
    path = snapshot_dir / f"{source_id}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"  saved {len(out)} artifacts → {path}")


def _load_snapshot(snapshot_dir: Path, source_id: str) -> list[RawArtifact]:
    """Load raw artifacts from a frozen snapshot on disk."""
    path = snapshot_dir / f"{source_id}.json"
    if not path.exists():
        raise FileNotFoundError(f"Snapshot file not found: {path}")
    raw = json.loads(path.read_text())
    artifacts = []
    for item in raw:
        artifacts.append(
            RawArtifact(
                payload=bytes.fromhex(
                    item["payload_hex"] if "payload_hex" in item else item["payload_b64"]
                ),
                source_url=item["source_url"],
                media_type=item["media_type"],
            )
        )
    print(f"  loaded {len(artifacts)} artifacts from {path}")
    return artifacts


def _lookup_tender_meta(source_id: str, subject_ref: str) -> dict:
    """Look up tender metadata for a flag's subject_ref using Django ORM.

    All queries are scoped by source_id to prevent cross-jurisdiction contamination.

    Handles three subject_ref formats:
    - tender_id (i001, i002, i004)
    - buyer→supplier (i003)
    - buyer_name (i005)
    """

    def _meta(t: Tender) -> dict:
        return {
            "title": t.title,
            "value_amount_cents": t.value_amount_cents,
            "currency": t.currency,
            "buyer_name": t.buyer_name,
            "procurement_method": t.procurement_method,
            "procurement_method_details": t.procurement_method_details,
        }

    qs = Tender.objects.filter(source_id=source_id)

    # Try direct tender_id lookup
    try:
        return _meta(qs.get(tender_id=subject_ref))
    except Tender.DoesNotExist:
        pass

    # i003: buyer→supplier format
    if "→" in subject_ref:
        buyer = subject_ref.split("→")[0].strip()
        tender = qs.filter(buyer_name__icontains=buyer).first()
        if tender:
            return _meta(tender)

    # i005: subject_ref is the buyer name
    tender = qs.filter(buyer_name__iexact=subject_ref).first()
    if tender:
        return _meta(tender)

    # Try buyer_name contains (for truncated names)
    tender = qs.filter(buyer_name__icontains=subject_ref[:50]).first()
    if tender:
        return _meta(tender)

    # Try colon-separated tender_id
    if ":" in subject_ref:
        tender_id = subject_ref.split(":")[0]
        try:
            return _meta(qs.get(tender_id=tender_id))
        except Tender.DoesNotExist:
            pass

    return {}


def run_experiment(
    sample_size: int = 200,
    snapshot_dir: Path | None = None,
    fetch: bool = True,
) -> dict:
    """Run the full kill experiment and return results as a dict.

    Args:
        sample_size: records per source (ignored if loading from snapshot)
        snapshot_dir: directory for frozen snapshot (save if fetch=True, load if fetch=False)
        fetch: if True, fetch live data and save to snapshot_dir; if False, load from snapshot_dir
    """
    from uncorrupt.staging.models import Award, Bid

    Tender.objects.all().delete()
    Award.objects.all().delete()
    Bid.objects.all().delete()

    snapshot_date = date.today().isoformat()
    if snapshot_dir is None:
        snapshot_dir = Path("experiments") / f"snapshot_{snapshot_date}"

    if fetch:
        print(f"Fetching {sample_size} records from each source...")
        print(f"  Snapshot dir: {snapshot_dir}")
    else:
        print(f"Loading frozen snapshot from {snapshot_dir}...")

    sources = [
        ("ua_prozorro", fetch_ua, "ua"),
        ("uk_contracts_finder", fetch_uk, "gb"),
        ("co_secop_ii", fetch_co, "co"),
    ]

    for source_id, fetcher, _locale in sources:
        print(f"  {source_id}...", end=" ", flush=True)
        try:
            if fetch:
                artifacts = fetcher(sample_size)
                _save_snapshot(snapshot_dir, source_id, artifacts)
            else:
                artifacts = _load_snapshot(snapshot_dir, source_id)
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
        IncorporationProximity(),
        ValueVsCompanySize(),
        DormancyDelinquency(),
    ]

    all_flags: list[dict[str, Any]] = []
    base_rate_table: list[dict[str, Any]] = []
    for locale_code, source_id in source_by_locale.items():
        locale = load_locale(locale_code)
        ctx = EvaluationContext(locale=locale, source_id=source_id)

        print(f"\nRunning indicators for {locale_code} ({source_id})...")
        for ind in indicators:
            if not ind.runs_in(locale_code):
                continue
            flags = list(ind.evaluate(ctx))
            units = ind.units_evaluated
            base_rate = len(flags) / units if units > 0 else 0.0
            discriminating = base_rate <= BASE_RATE_THRESHOLD

            base_rate_table.append(
                {
                    "source_id": source_id,
                    "indicator_id": ind.id,
                    "flags_emitted": len(flags),
                    "units_evaluated": units,
                    "base_rate": round(base_rate, 4),
                    "discriminating": discriminating,
                }
            )

            if flags:
                rate_str = f"{base_rate:.0%}" if units > 0 else "N/A"
                disc_str = "yes" if discriminating else "NO (>20%)"
                print(f"  {ind.id}: {len(flags)} flags ({rate_str} base rate, {disc_str})")
                for f in flags:
                    meta = _lookup_tender_meta(source_id, f.subject_ref)
                    explanation = f.explanation
                    if not discriminating and units > 0:
                        explanation += f" [WEAK: base rate {base_rate:.0%}]"
                    all_flags.append(
                        {
                            "indicator_id": f.indicator_id,
                            "subject_ref": f.subject_ref,
                            "as_of": f.as_of.isoformat(),
                            "explanation": explanation,
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
                            "base_rate": round(base_rate, 4),
                            "discriminating": discriminating,
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

    # Base-rate table — the key deliverable: which indicators are valid where.
    print(f"\n{'=' * 60}")
    print("BASE-RATE TABLE (flags / units_evaluated / base_rate / discriminating)")
    print(f"{'=' * 60}")
    print(f"{'Source':<22} {'Indicator':<28} {'Flags':>6} {'Units':>6} {'Rate':>7} {'Discr.':>8}")
    print("-" * 80)
    for entry in base_rate_table:
        rate_str = f"{entry['base_rate']:.0%}" if entry["units_evaluated"] > 0 else "N/A"
        disc_str = "yes" if entry["discriminating"] else "NO"
        print(
            f"{entry['source_id']:<22} {entry['indicator_id']:<28} "
            f"{entry['flags_emitted']:>6} {entry['units_evaluated']:>6} "
            f"{rate_str:>7} {disc_str:>8}"
        )

    print(f"\nTotal flags: {len(all_flags)}")
    print("Fewer than 10 is a valid result — not failure (ADR-002 D5).")

    return {
        "experiment_date": date.today().isoformat(),
        "snapshot_date": snapshot_date,
        "snapshot_dir": str(snapshot_dir),
        "sample_size_per_source": sample_size,
        "fetched_at": datetime.now().isoformat() if fetch else "loaded-from-snapshot",
        "total_flags": len(all_flags),
        "by_indicator": by_indicator,
        "by_jurisdiction": by_jurisdiction,
        "base_rate_table": base_rate_table,
        "flags": all_flags,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the comparative kill experiment.")
    parser.add_argument("--sample-size", type=int, default=200, help="Records per source")
    parser.add_argument(
        "--output",
        default="experiments/flags_raw.json",
        help="Output file for flags",
    )
    parser.add_argument(
        "--snapshot-dir",
        default=None,
        help=(
            "Snapshot directory (auto-generated if not specified). "
            "Use with --load to reuse a frozen snapshot."
        ),
    )
    parser.add_argument(
        "--load",
        action="store_true",
        help="Load from frozen snapshot instead of fetching live data.",
    )
    args = parser.parse_args()

    snapshot_dir = Path(args.snapshot_dir) if args.snapshot_dir else None
    results = run_experiment(
        sample_size=args.sample_size,
        snapshot_dir=snapshot_dir,
        fetch=not args.load,
    )

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nFlags exported to {output_path}")

    # Exit 0 always — fewer than 10 flags is valid, not failure (ADR-002 D5)
    print(f"\nTotal flags: {results['total_flags']}")


if __name__ == "__main__":
    main()
