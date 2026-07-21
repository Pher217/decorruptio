"""A non-redistributable source (OpenSanctions, CC-BY-NC) must never reach a bulk
open export (ADR-001 D5-bis)."""

from datetime import datetime

import pytest

from uncorrupt.core.errors import RedistributionViolation
from uncorrupt.core.provenance import ProvenanceRecord, Redistribution
from uncorrupt.core.tiers import DataClass, Tier
from uncorrupt.register.enforcement import assert_bulk_open_exportable, filter_bulk_open_exportable


def _rec(redist: Redistribution, tier: Tier) -> ProvenanceRecord:
    return ProvenanceRecord(
        source_id="opensanctions", source_url="https://opensanctions.org",
        retrieved_at=datetime(2026, 7, 21), content_hash="x" * 64,
        license="CC-BY-NC", redistribution=redist, jurisdiction="GLOBAL",
        data_class=DataClass.A2, tier=tier, connector="opensanctions", connector_version="0",
    )


def test_non_commercial_is_rejected_from_open_export():
    with pytest.raises(RedistributionViolation):
        assert_bulk_open_exportable(_rec(Redistribution.NON_COMMERCIAL, Tier.A))


def test_filter_drops_non_redistributable():
    recs = [
        _rec(Redistribution.OPEN, Tier.A),
        _rec(Redistribution.NON_COMMERCIAL, Tier.A),
    ]
    assert len(filter_bulk_open_exportable(recs)) == 1
