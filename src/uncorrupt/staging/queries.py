"""Query helpers for the DuckDB staging layer.

Used by indicators to retrieve tenders, awards, and bids in a unified format.
"""

from __future__ import annotations

from typing import Any

import duckdb


def get_tenders(
    conn: duckdb.DuckDBPyConnection,
    source_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Retrieve tenders, optionally filtered by source."""
    if source_id:
        rows = conn.execute(
            "SELECT * FROM tenders WHERE source_id = ? LIMIT ?",
            [source_id, limit],
        ).fetchall()
        cols = [d[0] for d in conn.description]
    else:
        rows = conn.execute("SELECT * FROM tenders LIMIT ?", [limit]).fetchall()
        cols = [d[0] for d in conn.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]


def get_awards(
    conn: duckdb.DuckDBPyConnection,
    source_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Retrieve awards, optionally filtered by source."""
    if source_id:
        rows = conn.execute(
            "SELECT * FROM awards WHERE source_id = ? LIMIT ?",
            [source_id, limit],
        ).fetchall()
        cols = [d[0] for d in conn.description]
    else:
        rows = conn.execute("SELECT * FROM awards LIMIT ?", [limit]).fetchall()
        cols = [d[0] for d in conn.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]


def get_bids(
    conn: duckdb.DuckDBPyConnection,
    source_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Retrieve bids, optionally filtered by source."""
    if source_id:
        rows = conn.execute(
            "SELECT * FROM bids WHERE source_id = ? LIMIT ?",
            [source_id, limit],
        ).fetchall()
        cols = [d[0] for d in conn.description]
    else:
        rows = conn.execute("SELECT * FROM bids LIMIT ?", [limit]).fetchall()
        cols = [d[0] for d in conn.description]
    return [dict(zip(cols, r, strict=False)) for r in rows]
