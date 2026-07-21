"""Reusable conformance suite every connector must pass (used by connector-gate.yml).

Kept import-light so CI can run it against a single changed connector.
"""

from __future__ import annotations

from uncorrupt.connectors.base import Connector
from uncorrupt.register.loader import load_source


def check_connector(conn: Connector) -> list[str]:
    """Return a list of conformance problems ([] means conformant)."""
    problems: list[str] = []
    if not getattr(conn, "source_id", ""):
        problems.append("connector has no source_id")
        return problems
    try:
        load_source(conn.source_id)
    except Exception as exc:  # noqa: BLE001
        problems.append(f"no valid register entry: {exc}")
    if not getattr(conn, "jurisdictions", None):
        problems.append("connector declares no jurisdictions")
    if not hasattr(conn, "discover") or not hasattr(conn, "fetch"):
        problems.append("connector does not implement discover/fetch")
    return problems
