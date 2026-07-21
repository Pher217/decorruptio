"""Connector discovery via the 'uncorrupt.connectors' entry-point group.

Binds each connector to its register entry and enforces the A2/DPIA gate at load.
"""

from __future__ import annotations

from importlib.metadata import entry_points

from uncorrupt.connectors.base import Connector
from uncorrupt.core.errors import RegisterError
from uncorrupt.core.tiers import DataClass
from uncorrupt.register.loader import load_source

_GROUP = "uncorrupt.connectors"


def load_connectors() -> dict[str, Connector]:
    out: dict[str, Connector] = {}
    for ep in entry_points(group=_GROUP):
        conn: Connector = ep.load()()
        entry = load_source(conn.source_id)
        if conn.data_class is DataClass.A2 and not entry.dpia_cleared:
            raise RegisterError(
                f"connector {conn.source_id} is A2 but sources/{conn.source_id}.yml"
                " has dpia_cleared: false — refusing to load"
            )
        out[conn.source_id] = conn
    return out
