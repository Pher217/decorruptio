"""run_indicators.py -- the entry point that actually persists indicator flags.

Every prior indicator run was an ad-hoc script that printed or exported JSON
(`kill_experiment.py`, `indicator_kill_test.py`, ...) — none of them called
`registry.enabled_for()` and wrote to `staging.Flag`, so `staging_flag` holds
0 rows and no published number is checkable from the database. This script
closes that gap: it runs `enabled_for(locale)` (never `load_indicators()`
directly — that is what keeps a `ValidationStatus.UNVALIDATED` indicator like
i009_contract_splitting for gb/ua/co from ever running in a normal scoring
pass) and persists every yielded `Flag` dataclass to the `Flag` model.

Idempotency scoping: `Flag` carries `source_id`, so the delete and the
persisted-row integrity check are both scoped to `(source_id, indicator_id)`.
Re-running the same source replaces that source's flags for the evaluated
indicators only; a second source's flags are never touched.

Also note: not every Flag.subject_ref resolves to a Tender (i003/i005/i009
use buyer/buyer-supplier keys, not a tender-shaped ref) so `tender_ref` is
best-effort and legitimately None for those.

Usage:
    uv run python scripts/run_indicators.py
    uv run python scripts/run_indicators.py --source uk_contracts_finder --locale gb
    uv run python scripts/run_indicators.py --dry-run --output experiments/run_report.json
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
from pathlib import Path
from typing import Any

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.base")
django.setup()

from django.db import transaction  # noqa: E402

from uncorrupt.indicators.base import Indicator  # noqa: E402
from uncorrupt.indicators.context import EvaluationContext  # noqa: E402
from uncorrupt.indicators.registry import enabled_for, load_indicators  # noqa: E402
from uncorrupt.register.loader import load_locale  # noqa: E402
from uncorrupt.staging.models import Flag as FlagModel  # noqa: E402
from uncorrupt.staging.models import Tender  # noqa: E402


def _to_jsonable(value: Any) -> Any:
    """Serialise a dataclass (or any object json.dumps chokes on, e.g. a date)
    into a JSON-safe structure, matching what the JSONField columns need since
    Flag.evidence_json/stamp_json use the plain (non-Django) JSON encoder."""
    return json.loads(json.dumps(value, default=str))


def _resolve_tender(source_id: str, subject_ref: str) -> Tender | None:
    """Best-effort: most indicators encode subject_ref as "tender_id" or
    "tender_id:award_id" (i004/i006/i007/i008; i001/i002 have no colon, which
    is just the bare tender_id). i003/i005/i009 use buyer or buyer->supplier
    keys that never match a tender_id -- the lookup simply returns None."""
    tender_id = subject_ref.split(":", 1)[0]
    return Tender.objects.filter(source_id=source_id, tender_id=tender_id).first()


def run(source: str, locale_code: str, *, dry_run: bool = False) -> dict[str, Any]:
    locale = load_locale(locale_code)
    ctx = EvaluationContext(locale=locale, source_id=source)

    all_indicators: dict[str, Indicator] = load_indicators()
    ran: list[Indicator] = enabled_for(locale_code)
    ran_ids = [i.id for i in ran]
    skipped_unvalidated = sorted(iid for iid in all_indicators if iid not in ran_ids)

    per_indicator: dict[str, dict[str, int]] = {}

    with transaction.atomic():
        # Idempotency: wipe this source/run's scope before inserting, so re-running
        # never duplicates rows and a second source never deletes another source's flags.
        FlagModel.objects.filter(source_id=source, indicator_id__in=ran_ids).delete()

        for indicator in ran:
            flags = list(indicator.evaluate(ctx))
            rows = [
                FlagModel(
                    source_id=source,
                    indicator_id=f.indicator_id,
                    subject_ref=f.subject_ref,
                    as_of=f.as_of,
                    explanation=f.explanation,
                    evidence_json=_to_jsonable([dataclasses.asdict(e) for e in f.evidence]),
                    stamp_json=_to_jsonable(dataclasses.asdict(f.stamp)),
                    tender_ref=_resolve_tender(source, f.subject_ref),
                )
                for f in flags
            ]
            persisted = 0
            if not dry_run:
                FlagModel.objects.bulk_create(rows)
                persisted = len(rows)
            per_indicator[indicator.id] = {
                "flags": len(flags),
                "persisted": persisted,
                "units_evaluated": indicator.units_evaluated,
                "units_unscoreable": getattr(indicator, "units_unscoreable", 0),
            }

        total_persisted = sum(v["persisted"] for v in per_indicator.values())

        # The integrity check MUST run inside the transaction. Outside it, the rows are
        # already committed by the time the count runs, so a mismatch raises loudly over
        # data that is already durable -- an alarm, not a guard. Inside, the count sees
        # this transaction's own rows and the raise rolls the whole run back.
        if not dry_run:
            actual_persisted = FlagModel.objects.filter(
                source_id=source, indicator_id__in=ran_ids
            ).count()
            if actual_persisted != total_persisted:
                raise AssertionError(
                    f"persisted-row count mismatch: db has {actual_persisted} Flag rows "
                    f"for {ran_ids}, report computed {total_persisted}. This is the exact "
                    "divergence this runner exists to prevent -- do not paper over it."
                )

        if dry_run:
            transaction.set_rollback(True)

    total_flags = sum(v["flags"] for v in per_indicator.values())

    return {
        "source": source,
        "locale": locale_code,
        "dry_run": dry_run,
        "ran": ran_ids,
        "skipped_unvalidated": skipped_unvalidated,
        "per_indicator": per_indicator,
        "totals": {"flags": total_flags, "persisted": total_persisted},
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run registered indicators for a source and persist their flags."
    )
    parser.add_argument("--source", default="uk_contracts_finder")
    parser.add_argument("--locale", default="gb")
    parser.add_argument("--output", type=Path, default=None, help="Optional JSON report path")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Evaluate and report but persist nothing",
    )
    args = parser.parse_args(argv)

    report = run(args.source, args.locale, dry_run=args.dry_run)

    print(f"source={report['source']} locale={report['locale']} dry_run={report['dry_run']}")
    print(f"ran: {', '.join(report['ran']) or '(none)'}")
    print(f"skipped (unvalidated): {', '.join(report['skipped_unvalidated']) or '(none)'}")
    for indicator_id, stats in report["per_indicator"].items():
        print(
            f"  {indicator_id}: flags={stats['flags']} persisted={stats['persisted']} "
            f"units_evaluated={stats['units_evaluated']} "
            f"units_unscoreable={stats['units_unscoreable']}"
        )
    print(f"totals: flags={report['totals']['flags']} persisted={report['totals']['persisted']}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"report -> {args.output}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
