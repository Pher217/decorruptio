"""Provenance + version stamp are mandatory on flags (ADR-000 G6). Structural check:
the Flag type requires evidence + stamp fields."""

import dataclasses

from uncorrupt.indicators.base import Flag


def test_flag_requires_evidence_and_stamp():
    fields = {f.name for f in dataclasses.fields(Flag)}
    assert {"evidence", "stamp", "explanation", "as_of"} <= fields
