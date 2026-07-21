"""A tier-b/tier-c record can never reach a tier-a open export (ADR-000 G2)."""

import pytest

from uncorrupt.core.errors import TierViolation
from uncorrupt.core.tiers import Tier, assert_exportable_to_open


def test_tier_b_blocked():
    with pytest.raises(TierViolation):
        assert_exportable_to_open(Tier.B)


def test_tier_c_blocked():
    with pytest.raises(TierViolation):
        assert_exportable_to_open(Tier.C)


def test_tier_a_allowed():
    assert_exportable_to_open(Tier.A)  # no raise
