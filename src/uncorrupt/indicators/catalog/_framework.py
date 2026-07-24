"""Framework/DPS detection — shared by i001 and i004.

Framework agreements and Dynamic Purchasing Systems (DPS) are single-supplier
or ceiling-based by design. Flagging them as single-bidder or price-deviation
is a false positive — the procedure was never meant to be competitive in the
award moment, or the "estimate" is a ceiling, not a market-price estimate.
"""

from __future__ import annotations

import json
from typing import Any

# Keywords that indicate a framework/DPS establishment or call-off
# (checked case-insensitively against title + method + method_details).
FRAMEWORK_KEYWORDS = [
    "framework",
    "dynamic purchasing",
    "dps",
    "call-off",
    "call off",
    "supplier list",
]


def is_framework_or_dps(
    title: str | None,
    procurement_method: str | None,
    procurement_method_details: str | None,
    raw_json: dict[str, Any] | None = None,
) -> bool:
    """Return True if the tender is a framework/DPS establishment or call-off.

    Checks:
    (a) tender title for framework/DPS markers
    (b) procurement_method + procurement_method_details for framework/call-off
    (c) OCDS tender.techniques.hasFrameworkAgreement in raw_json when present
    """
    text = f"{title or ''} {procurement_method or ''} {procurement_method_details or ''}".lower()
    if any(kw in text for kw in FRAMEWORK_KEYWORDS):
        return True

    # OCDS structured field: tender.techniques.hasFrameworkAgreement
    if raw_json:
        techniques = (
            raw_json.get("tender", {}).get("techniques")
            if isinstance(raw_json.get("tender"), dict)
            else raw_json.get("techniques")
        )
        if isinstance(techniques, dict) and techniques.get("hasFrameworkAgreement"):
            return True

    return False


def _raw_json_to_dict(raw: Any) -> dict[str, Any] | None:
    """Coerce a raw_json field (may be str, dict, or None) to dict."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else None
        except (ValueError, TypeError):
            return None
    return None
