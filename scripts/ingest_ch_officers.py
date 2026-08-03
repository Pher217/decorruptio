"""Fetch + ingest Companies House officers records (Phase 1.4).

Fetches officers for a bounded list of company numbers from the CH REST
API (`/company/{company_number}/officers`), caching each company's
(personal-data-stripped) response under `experiments/` with a provenance
record, then ingests the cache into the graph app's Entity/Edge tables.

Requires a free API key exported as `COMPANIES_HOUSE_API_KEY`. See
`uncorrupt.graph.ch_officers` module docstring for scope boundaries
(company-officer capacity only; DOB/address/nationality dropped).

Coverage expansion, made scientifically defensible rather than a
manufactured result:

- **The company set must not be derived from the benchmark.** Use
  `--universe procurement-suppliers` to draw the candidate set from
  `staging.SupplierResolution` (every CH company a procurement-corpus
  supplier was already resolved to) -- defined without any reference to
  which rows are positive or negative benchmark cases. `--company-number`
  / `--company-numbers-file` remain available for an explicit list, but
  that list must not be assembled by filtering on benchmark membership.
- **Traversal order must not correlate with a real-world company
  attribute.** The candidate set is always reordered via a salted hash
  (`sha256(salt + company_number)`) before `--limit` is applied --
  company-number-ascending order correlates with incorporation cohort and
  would bias a partial sweep towards older companies. The salt is
  auto-generated on a sweep's first invocation and then persisted in
  `{output_dir}/run_manifest.jsonl`, so every later partial run of the
  same sweep reuses it automatically (pass `--salt` to pin one
  explicitly).
- **`--limit N`** bounds one invocation to the next N candidates (in that
  hash order) that have not already been attempted (CH allows 600
  requests / 5 minutes, so a full sweep runs as many resumable partial
  invocations, not one). Re-running with the same candidate set picks up
  where the previous invocation left off.
- Every run appends a record to the manifest (selection rule, salt,
  timestamp, counts) so a partial run's history is auditable.
- `--expand-appointments` walks exactly one additional appointment
  frontier for the officers discovered in this run's batch, then stops --
  no recursive re-expansion.

Usage:
    uv run python scripts/ingest_ch_officers.py --universe procurement-suppliers --limit 500
    uv run python scripts/ingest_ch_officers.py --company-numbers-file numbers.txt --limit 500
    uv run python scripts/ingest_ch_officers.py --company-number 12410514
    uv run python scripts/ingest_ch_officers.py --report-coverage
"""

from __future__ import annotations

import argparse
import json
import os
import secrets

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from pathlib import Path  # noqa: E402

from uncorrupt.graph.ch_appointments import (  # noqa: E402
    fetch_officer_appointments,
    ingest_officer_appointments,
)
from uncorrupt.graph.ch_officers import (  # noqa: E402
    DEFAULT_MAX_CACHE_AGE_DAYS,
    append_run_manifest,
    coverage_report,
    fetch_company_officers,
    ingest_company_officers,
    officer_ids_for_companies,
    procurement_supplier_universe,
    procurement_universe_coverage_report,
    salted_hash_order,
    select_next_pending,
)

DEFAULT_OUTPUT_DIR = "experiments/ch_officers"
DEFAULT_APPOINTMENTS_OUTPUT_DIR = "experiments/ch_appointments"


def _read_company_numbers(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def _load_or_create_salt(output_dir: Path, explicit_salt: str | None) -> str:
    """Return the salt to use for this sweep's hash-ordered traversal.

    An explicit `--salt` always wins. Otherwise, reuse the salt recorded
    by this sweep's earliest manifest entry, so every partial run of the
    same sweep samples against the same precommitted order. Only when no
    manifest exists yet -- this sweep's first invocation -- is a new salt
    generated and (via `append_run_manifest`) pinned for every later run.
    """
    if explicit_salt:
        return explicit_salt
    manifest_path = output_dir / "run_manifest.jsonl"
    if manifest_path.exists():
        for line in manifest_path.read_text().splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            if record.get("salt"):
                return str(record["salt"])
    return secrets.token_hex(16)


def _print_coverage_reports() -> None:
    print("=== GB-COH officer coverage (all graph entities) ===")
    for key, value in coverage_report().items():
        print(f"  {key}: {value:,}")
    print("\n=== GB-COH officer coverage (procurement-supplier universe) ===")
    for key, value in procurement_universe_coverage_report().items():
        print(f"  {key}: {value:,}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--company-numbers-file",
        help="Path to a file with one Companies House company number per line.",
    )
    parser.add_argument(
        "--company-number",
        action="append",
        dest="company_numbers",
        help="A single company number (repeatable).",
    )
    parser.add_argument(
        "--universe",
        choices=["procurement-suppliers"],
        help="Draw the candidate set from a pre-registered, benchmark-independent universe "
        "instead of an explicit list. 'procurement-suppliers': every CH company a "
        "procurement-corpus supplier was already resolved to (staging.SupplierResolution). "
        "Mutually exclusive with --company-number/--company-numbers-file.",
    )
    parser.add_argument(
        "--salt",
        help="Pin the salted-hash traversal order explicitly. Auto-generated and persisted "
        "in the run manifest on a sweep's first invocation if omitted.",
    )
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="Skip the download and ingest an existing cache in --output-dir.",
    )
    parser.add_argument(
        "--max-age-days",
        type=int,
        default=DEFAULT_MAX_CACHE_AGE_DAYS,
        help="Refetch a cached company response older than this many days "
        f"(default {DEFAULT_MAX_CACHE_AGE_DAYS}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Bound this run to the next N candidate companies (salted-hash order) that "
        "have not already been attempted, so a full sweep can be split into resumable "
        "partial runs.",
    )
    parser.add_argument(
        "--expand-appointments",
        action="store_true",
        help="After ingesting, walk one additional appointment frontier for the officers "
        "discovered in this run's batch (ch_appointments), then stop.",
    )
    parser.add_argument("--appointments-output-dir", default=DEFAULT_APPOINTMENTS_OUTPUT_DIR)
    parser.add_argument(
        "--report-coverage",
        action="store_true",
        help="Print GB-COH officer coverage counts (direct roster fetch / appointment-hop "
        "only / zero officers), both overall and for the procurement-supplier universe, "
        "and exit without fetching or ingesting anything.",
    )
    args = parser.parse_args()

    if args.report_coverage:
        _print_coverage_reports()
        return

    explicit_numbers = list(args.company_numbers or [])
    if args.company_numbers_file:
        explicit_numbers.extend(_read_company_numbers(args.company_numbers_file))

    if args.universe and explicit_numbers:
        parser.error("--universe cannot be combined with --company-number/--company-numbers-file")
    if args.universe:
        company_numbers = procurement_supplier_universe()
        selection_rule = f"universe={args.universe}"
    elif explicit_numbers:
        company_numbers = explicit_numbers
        selection_rule = (
            f"explicit (company_numbers_file={args.company_numbers_file!r}, "
            f"inline_count={len(args.company_numbers or [])})"
        )
    else:
        parser.error("provide --company-number, --company-numbers-file, or --universe")

    output_dir = Path(args.output_dir)
    salt = _load_or_create_salt(output_dir, args.salt)
    ordered = salted_hash_order(company_numbers, salt)
    batch = select_next_pending(ordered, output_dir, args.limit, args.max_age_days)
    print(
        f"selected {len(batch)} of {len(company_numbers)} candidate companies for this run "
        f"(limit={args.limit if args.limit is not None else 'none'}, salt={salt})"
    )

    if not batch:
        print("nothing to do -- every candidate already has a valid cache entry")
        return

    fetched = cached = 0
    if not args.skip_fetch:
        results = fetch_company_officers(batch, output_dir, max_cache_age_days=args.max_age_days)
        fetched = sum(1 for r in results if not r.cached)
        cached = sum(1 for r in results if r.cached)
        print(f"Fetched {fetched} companies ({cached} already cached) -> {output_dir}")

    summary = ingest_company_officers(batch, output_dir)
    print(
        f"Ingested: {summary['edges_created']} officer edges "
        f"({summary['officers_no_id']} without a stable officer ID), "
        f"{summary['companies_unmatched']} companies unmatched "
        f"(of {summary['companies_processed']} processed, "
        f"{summary['total_officers']} officer records, "
        f"{summary['missing_appointed_on']} missing appointed_on)"
    )

    append_run_manifest(
        output_dir,
        selection_rule=selection_rule,
        salt=salt,
        limit=args.limit,
        candidate_count=len(company_numbers),
        attempted_companies=batch,
        fetched=fetched,
        cached=cached,
        ingest_summary=summary,
    )

    if args.expand_appointments:
        officer_ids = officer_ids_for_companies(batch)
        print(
            f"expanding {len(officer_ids)} officers discovered in this batch "
            "to their other appointments (single frontier)"
        )
        if officer_ids:
            appointments_output_dir = Path(args.appointments_output_dir)
            counts = fetch_officer_appointments(officer_ids, appointments_output_dir)
            print(
                f"appointments fetched {counts['fetched']}, cached {counts['cached']}, "
                f"failed {counts['failed']}"
            )
            appointment_stats = ingest_officer_appointments(officer_ids, appointments_output_dir)
            print(
                f"Appointments ingested: {appointment_stats['edges_created']} officer_of edges "
                f"from {appointment_stats['officers_processed']} officers "
                f"({appointment_stats['company_unmatched']} unmatched companies, "
                f"{appointment_stats['officer_missing']} officers not in graph)"
            )
            append_run_manifest(
                appointments_output_dir,
                selection_rule=(
                    "single appointment frontier for officers discovered in companies "
                    f"batch of {len(batch)} (see {output_dir}/run_manifest.jsonl)"
                ),
                salt=salt,
                requested_officers=len(officer_ids),
                fetch_counts=counts,
                ingest_summary=appointment_stats,
            )


if __name__ == "__main__":
    main()
