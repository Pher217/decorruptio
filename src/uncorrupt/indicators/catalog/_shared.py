"""Shared helpers for the indicator catalog."""

from __future__ import annotations

from typing import Any


def confidence_note(match_confidence: float, match_method: str | None) -> str:
    """Format a resolution-confidence caveat for a flag's explanation text.

    Always prints method and confidence — including for identifier matches.
    Suppressing the caveat for `method == "identifier"` (the previous
    per-indicator behaviour) hid the caveat exactly where a misattributed
    flag would print it: an identifier match can still be wrong if the
    award's own GB-COH id was misresolved upstream.
    """
    return f" [match_confidence={match_confidence:.1f}, method={match_method}]"


def confidence_note_from_resolution(res: dict[str, Any]) -> str:
    """Convenience wrapper for the `{"confidence": ..., "method": ...}` dict shape
    used by the per-award resolution lookups in the i006/i007/i008 indicators."""
    return confidence_note(res["confidence"], res["method"])
