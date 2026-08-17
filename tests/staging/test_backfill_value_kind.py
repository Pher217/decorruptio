"""Migration 0007 backfills value_kind on rows that predate 0006.

0006 added the column with default="per_supplier" and no data migration, so every
pre-existing row was labelled per-supplier regardless of what it is. The existing
value_kind tests never caught that: they either ingest fresh data (which classifies
correctly) or assign value_kind by hand. Nothing exercised the migration path, so the
wrong implementation -- add the column, backfill nothing -- passed the whole suite.

These tests exercise the backfill function itself against real Award rows.
"""

from __future__ import annotations

import importlib.util
import pathlib
from datetime import UTC, datetime

from django.apps import apps as global_apps
from django.test import TestCase

# The migration module name starts with a digit, so it cannot be imported normally.
# Load the real file -- testing a copy of the logic would defeat the purpose.
import uncorrupt.staging.migrations as _migrations_pkg
from uncorrupt.staging.models import Award, Tender

_spec = importlib.util.spec_from_file_location(
    "migration_0007",
    pathlib.Path(_migrations_pkg.__file__).parent / "0007_backfill_award_value_kind.py",
)
migration_0007 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(migration_0007)
backfill_for_tests = migration_0007.backfill

SOURCE = "uk_contracts_finder"


def _award(award_id: str, *, suppliers: object, value_kind: str = "per_supplier") -> Award:
    tender, _ = Tender.objects.get_or_create(
        source_id=SOURCE, tender_id=f"t-{award_id}", defaults={"source_url": "https://example.com"}
    )
    return Award.objects.create(
        source_id=SOURCE,
        tender_id=f"t-{award_id}",
        award_id=award_id,
        tender_ref=tender,
        supplier_name="Supplier Ltd",
        currency="GBP",
        value_amount_cents=1_000_00,
        value_kind=value_kind,
        status="active",
        award_date=datetime(2024, 6, 15, tzinfo=UTC),
        raw_json={"suppliers": suppliers} if suppliers is not None else {},
    )


class BackfillValueKindTest(TestCase):
    def test_multi_supplier_row_is_reclassified_as_shared_ceiling(self) -> None:
        """GIVEN a pre-0006 Award row defaulted to per_supplier whose payload has 2 suppliers
        WHEN the 0007 backfill runs
        THEN that row becomes shared_ceiling."""
        a = _award("multi", suppliers=[{"id": "1"}, {"id": "2"}])
        backfill_for_tests(global_apps, None)
        a.refresh_from_db()
        assert a.value_kind == "shared_ceiling"

    def test_single_supplier_row_is_left_as_per_supplier(self) -> None:
        """GIVEN a row whose payload has exactly 1 supplier
        WHEN the backfill runs
        THEN it stays per_supplier."""
        a = _award("single", suppliers=[{"id": "1"}])
        backfill_for_tests(global_apps, None)
        a.refresh_from_db()
        assert a.value_kind == "per_supplier"

    def test_empty_supplier_list_is_per_supplier_matching_ingest(self) -> None:
        """GIVEN a row whose payload has an empty supplier list
        WHEN the backfill runs
        THEN it is per_supplier, because ingest's rule is `len(...) > 1`, so 0 is per_supplier.
        Treating 0 as 'leave alone' would silently diverge from the rule being reproduced."""
        a = _award("empty", suppliers=[])
        backfill_for_tests(global_apps, None)
        a.refresh_from_db()
        assert a.value_kind == "per_supplier"

    def test_unreadable_payload_is_left_untouched_and_does_not_raise(self) -> None:
        """GIVEN a row whose raw_json carries no supplier list at all
        WHEN the backfill runs
        THEN the row is untouched and no exception is raised."""
        a = _award("bad", suppliers=None, value_kind="shared_ceiling")
        backfill_for_tests(global_apps, None)
        a.refresh_from_db()
        assert a.value_kind == "shared_ceiling"

    def test_backfill_is_idempotent(self) -> None:
        """GIVEN the backfill has already run
        WHEN it runs a second time
        THEN values are unchanged."""
        a = _award("multi2", suppliers=[{"id": "1"}, {"id": "2"}])
        backfill_for_tests(global_apps, None)
        backfill_for_tests(global_apps, None)
        a.refresh_from_db()
        assert a.value_kind == "shared_ceiling"
