"""Verify every `label_source_url` / `label_source_quote` pair in a gold manifest.

Usage:
    uv run python scripts/verify_citations.py --manifest path/to/manifest.json
    uv run python scripts/verify_citations.py --manifest path/to/manifest.csv \
        --cache-dir experiments/citation_cache

Reads a manifest-shaped JSON (a list of row objects) or CSV, checks every
row's `label_source_url` / `label_source_quote` pair with
`uncorrupt.research.citation_verifier.verify_citation`, and prints a
per-row result plus a summary.

This exists because an overnight benchmark-sourcing pass found 9/34 gold
manifest rows (26%) defective on adversarial human review -- including two
quotes in quotation marks that never appear in the cited article. That
verification does not scale to a new gold manifest per country; this script
makes it a repeatable, deterministic check instead of a one-off manual pass.

Exit code is non-zero only if any row is ABSENT (the fabricated/incorrect-
quote defect this tool exists to catch). UNFETCHABLE rows are reported but
do not fail the run on their own -- a blocked fetch is a retrieval problem,
not evidence the quote is wrong (see `citation_verifier` module docstring
for why the two are never conflated).

Read-only research tool: no Django, no database, no graph writes.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from uncorrupt.research.citation_verifier import (
    DEFAULT_MAX_CACHE_AGE_DAYS,
    DEFAULT_NEAR_THRESHOLD,
    CitationStatus,
    verify_citation,
)

DEFAULT_CACHE_DIR = "experiments/citation_cache"


def _load_rows(manifest_path: Path) -> list[dict[str, Any]]:
    """Load a manifest-shaped JSON list or CSV into a list of row dicts."""
    if manifest_path.suffix.lower() == ".csv":
        with open(manifest_path, newline="", encoding="utf-8") as f:
            return list(csv.DictReader(f))
    with open(manifest_path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(
            f"{manifest_path}: expected a JSON list of rows, got {type(data).__name__}"
        )
    return data


def _row_key(row: dict[str, Any], index: int) -> str:
    return str(row.get("case_id") or row.get("id") or index)


def verify_manifest(
    rows: list[dict[str, Any]],
    *,
    cache_dir: str | Path | None,
    near_threshold: float = DEFAULT_NEAR_THRESHOLD,
    max_cache_age_days: int = DEFAULT_MAX_CACHE_AGE_DAYS,
    client: httpx.Client | None = None,
) -> list[dict[str, Any]]:
    """Verify every row's label_source_url/label_source_quote pair.

    Returns one report dict per row (input order), including rows with a
    missing url/quote reported as SKIPPED. Never raises on a defective row
    -- ABSENT and UNFETCHABLE are reported outcomes, not exceptions, so one
    bad row cannot abort the whole manifest.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True)
    reports: list[dict[str, Any]] = []
    try:
        for index, row in enumerate(rows):
            case_id = _row_key(row, index)
            url = row.get("label_source_url")
            quote = row.get("label_source_quote")
            if not url or not quote:
                reports.append(
                    {
                        "case_id": case_id,
                        "status": "SKIPPED",
                        "url": url,
                        "detail": "missing label_source_url or label_source_quote",
                    }
                )
                continue

            result = verify_citation(
                url,
                quote,
                client=client,
                cache_dir=cache_dir,
                max_cache_age_days=max_cache_age_days,
                near_threshold=near_threshold,
            )
            reports.append(
                {
                    "case_id": case_id,
                    "status": result.status.value,
                    "url": result.url,
                    "similarity": result.similarity,
                    "best_match": result.best_match,
                    "detail": result.detail,
                }
            )
    finally:
        if owns_client:
            client.close()
    return reports


def _print_report(reports: list[dict[str, Any]]) -> Counter:
    counts: Counter = Counter(r["status"] for r in reports)
    for r in reports:
        line = f"[{r['status']:21s}] {r['case_id']}"
        if r["status"] in (
            CitationStatus.NEAR.value,
            CitationStatus.ABSENT.value,
            CitationStatus.EXTRACTION_UNRELIABLE.value,
        ):
            similarity = r.get("similarity")
            line += f" (similarity={similarity:.2f})" if similarity is not None else ""
        if r["status"] in ("UNFETCHABLE", "EXTRACTION_UNRELIABLE", "SKIPPED"):
            line += f" -- {r.get('detail')}"
        print(line)
    print()
    print("Summary:", dict(counts))
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--manifest", required=True, type=Path, help="Path to a manifest JSON or CSV file"
    )
    parser.add_argument(
        "--cache-dir",
        default=DEFAULT_CACHE_DIR,
        help=f"Cache dir for fetched documents, gitignored (default: {DEFAULT_CACHE_DIR}). "
        "Pass an empty string to disable caching.",
    )
    parser.add_argument("--near-threshold", type=float, default=DEFAULT_NEAR_THRESHOLD)
    parser.add_argument("--max-cache-age-days", type=int, default=DEFAULT_MAX_CACHE_AGE_DAYS)
    parser.add_argument(
        "--output", type=Path, default=None, help="Also write the full JSON report to this path"
    )
    args = parser.parse_args(argv)

    rows = _load_rows(args.manifest)
    cache_dir = args.cache_dir or None
    reports = verify_manifest(
        rows,
        cache_dir=cache_dir,
        near_threshold=args.near_threshold,
        max_cache_age_days=args.max_cache_age_days,
    )

    counts = _print_report(reports)

    if args.output:
        args.output.write_text(json.dumps(reports, indent=2))
        print(f"Full report written to {args.output}")

    return 1 if counts.get(CitationStatus.ABSENT.value) else 0


if __name__ == "__main__":
    sys.exit(main())
