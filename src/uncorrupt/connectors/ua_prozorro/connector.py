"""Ukraine ProZorro connector — real fetch from the Open Procurement API.

Data is OCDS-like JSON: tenders with tenderPeriod, procuringEntity, bids,
awards, value, items. Near-OCDS but not a release package; the normalize
stage maps to canonical OCDS.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta

import httpx

from uncorrupt.connectors.base import Connector, FetchTask, RawArtifact
from uncorrupt.core.tiers import DataClass

API_BASE = "https://api.openprocurement.org/api/2.5"
PAGE_SIZE = 100


class UaProzorroConnector(Connector):
    source_id = "ua_prozorro"
    jurisdictions = ["UA"]
    data_class = DataClass.A1

    def discover(self, since: date | None = None) -> Iterator[FetchTask]:
        """Yield daily fetch tasks from `since` (default: yesterday)."""
        start = since or (date.today() - timedelta(days=1))
        offset = None
        while True:
            params: dict[str, str | int] = {
                "limit": PAGE_SIZE,
                "offset": start.isoformat(),
            }
            if offset:
                params["offset"] = offset

            resp = httpx.get(f"{API_BASE}/tenders", params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            page_ids = [item["id"] for item in data.get("data", [])]
            if not page_ids:
                break

            yield FetchTask(
                key=f"ua_prozorro:{start.isoformat()}:{offset or 'start'}",
                params={"tender_ids": page_ids, "date": start.isoformat()},
            )

            next_page = data.get("next_page", {})
            offset = next_page.get("offset")
            if not offset:
                break

    def fetch(self, task: FetchTask) -> Iterator[RawArtifact]:
        """Fetch full tender records for each ID in the task."""
        tender_ids: list[str] = task.params["tender_ids"]
        for tender_id in tender_ids:
            url = f"{API_BASE}/tenders/{tender_id}"
            resp = httpx.get(url, timeout=30)
            if resp.status_code != 200:
                continue
            yield RawArtifact(
                payload=resp.content,
                source_url=url,
                media_type="application/json",
            )
