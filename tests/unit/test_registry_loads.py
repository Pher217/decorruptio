"""The source register loads and validates, and the OpenSanctions entry is correctly
marked non-redistributable + A2/uncleared (so the guardrails have a real case)."""

from uncorrupt.core.provenance import Redistribution
from uncorrupt.core.tiers import DataClass
from uncorrupt.register.loader import all_sources, load_source


def test_all_sources_valid():
    ids = {s.source_id for s in all_sources()}
    assert {"eu_ted", "gleif", "opensanctions"} <= ids


def test_opensanctions_is_gated():
    s = load_source("opensanctions")
    assert s.data_class is DataClass.A2
    assert s.redistribution is Redistribution.NON_COMMERCIAL
    assert s.dpia_cleared is False
