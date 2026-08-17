"""Backfill `value_kind` on Award rows that predate 0006.

0006 added the column with `default="per_supplier"`, which is correct for a NEW row but
wrong for every row already in the table: classification happens only at ingest
(`ingest.py`, `"shared_ceiling" if len(suppliers_list) > 1 else "per_supplier"`), so
pre-existing rows were never classified at all.

Measured on the 2026-08-16 dev snapshot: 4,105 of 124,738 Award rows carry more than one
supplier in `raw_json`, i.e. 4,105 framework ceilings would sit in the database labelled
as per-supplier money -- the exact error ADR-013 exists to remove.

A full re-ingest also fixes them, so this migration is strictly a guard against the
migration and the rebuild being run as two separate operations. That separation is what
let 124,738 pre-fix rows survive a previous "re-ingest" (PR #30).

Reverse is a deliberate no-op: the pre-migration state is "unclassified", which is not a
state worth restoring, and the forward pass is idempotent.
"""

from __future__ import annotations

import json
from typing import Any

from django.db import migrations

BATCH_SIZE = 2_000


def _supplier_count(raw: Any) -> int | None:
    """Number of suppliers in a stored award payload, or None if unreadable.

    Reads `raw_json["suppliers"]` -- the same field, by the same name, that ingest reads
    when it decides value_kind. Any other source would be a second rule that could drift.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            return None
    if not isinstance(raw, dict):
        return None
    suppliers = raw.get("suppliers")
    if not isinstance(suppliers, list):
        return None
    return len(suppliers)


def backfill(apps, schema_editor) -> None:
    Award = apps.get_model("staging", "Award")

    updated = unchanged = unreadable = 0
    batch = []

    for award in (
        Award.objects.only("id", "value_kind", "raw_json")
        .order_by("id")
        .iterator(chunk_size=BATCH_SIZE)
    ):
        count = _supplier_count(award.raw_json)
        if count is None:
            unreadable += 1
            continue

        # Mirror ingest exactly, including the 0-supplier case: `0 > 1` is False there,
        # so an empty list means per_supplier. Treating it as "leave alone" would be a
        # silent divergence from the rule this migration claims to reproduce.
        expected = "shared_ceiling" if count > 1 else "per_supplier"
        if award.value_kind == expected:
            unchanged += 1
            continue

        award.value_kind = expected
        batch.append(award)
        updated += 1

        if len(batch) >= BATCH_SIZE:
            Award.objects.bulk_update(batch, ["value_kind"])
            batch = []

    if batch:
        Award.objects.bulk_update(batch, ["value_kind"])

    print(
        f"0007 backfill_award_value_kind: updated={updated} "
        f"unchanged={unchanged} unreadable={unreadable}"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("staging", "0006_award_value_kind"),
    ]

    operations = [
        migrations.RunPython(backfill, reverse_code=migrations.RunPython.noop),
    ]
