"""Cache + ingest a locally-downloaded OpenSanctions entities slice.

Ingests ONLY the non-personal slice (FtM schema Company/Organization/
LegalEntity) of OpenSanctions' free "default" collection bulk export —
see src/uncorrupt/graph/opensanctions.py's module docstring for the full
scope rationale and sources/opensanctions_entities.yml for the register
entry (a deliberately separate, narrower entry from sources/opensanctions.yml,
which stays A2/dpia_cleared:false/unused).

This script does NOT download anything itself: data.opensanctions.org's
robots.txt disallows automated fetching (see the register entry's
`rate_limit` note), so `--input-path` must point to a file already fetched
by a human via https://www.opensanctions.org/datasets/default/ (no login or
API key required for the free bulk export, per OpenSanctions' own docs).

Usage:
    uv run python scripts/ingest_opensanctions.py --input-path /path/to/entities.ftm.json
    uv run python scripts/ingest_opensanctions.py --input-path ... --skip-fetch  # re-ingest
        # an already-cached experiments/opensanctions/opensanctions_entities.jsonl
"""

from __future__ import annotations

import argparse
import os

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from uncorrupt.graph.opensanctions import SOURCE_ID, fetch_opensanctions, ingest_opensanctions
from uncorrupt.pipelines.run_recorder import Completeness, record_ingest_run

DEFAULT_OUTPUT_DIR = "experiments/opensanctions"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-path",
        default=None,
        help="Path to an already-downloaded OpenSanctions entities.ftm.json (JSONL). "
        "Required unless --skip-fetch.",
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip caching --input-path and re-ingest the existing cached JSONL in --output-dir.",
    )
    args = parser.parse_args()
    if not args.skip_fetch and not args.input_path:
        parser.error("--input-path is required unless --skip-fetch")

    jsonl_path = f"{args.output_dir}/opensanctions_entities.jsonl"

    with record_ingest_run(SOURCE_ID) as run:
        if not args.skip_fetch:
            fetch_result = fetch_opensanctions(args.input_path, args.output_dir)
            print(
                f"Cached {fetch_result.record_count} records -> {fetch_result.jsonl_path} "
                f"({fetch_result.content_hash})",
                flush=True,
            )
            jsonl_path = str(fetch_result.jsonl_path)
            records_fetched = fetch_result.record_count
        else:
            records_fetched = 0

        summary = ingest_opensanctions(jsonl_path)
        print(
            f"Ingested: {summary['created']} created, {summary['updated']} updated, "
            f"{summary['skipped_non_entity_schema']} skipped (Person/relationship schema), "
            f"{summary['skipped_no_id']} skipped (no id), "
            f"{summary['gleif_lei_linked']} linked to an existing GLEIF-LEI Entity, "
            f"{summary['gb_coh_linked']} linked to staging.Company via a GB registration "
            f"number (of {summary['total']} records read)",
            flush=True,
        )
        run.finish(
            Completeness.COMPLETE,
            records_fetched=records_fetched or summary["total"],
            records_ingested=summary["created"] + summary["updated"],
            detail=(
                f"skipped_non_entity_schema={summary['skipped_non_entity_schema']} "
                f"skipped_no_id={summary['skipped_no_id']} "
                f"gleif_lei_linked={summary['gleif_lei_linked']} "
                f"gb_coh_linked={summary['gb_coh_linked']}"
            ),
        )


if __name__ == "__main__":
    main()
