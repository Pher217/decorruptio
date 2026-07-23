"""UK Contracts Finder connector — real fetch from the OCDS Search API.

Returns native OCDS 1.1 release packages. Cursor-based pagination.
OGL v3.0 licence.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, timedelta

import httpx

from uncorrupt.connectors.base import Connector, FetchTask, RawArtifact
from uncorrupt.core.tiers import DataClass

API_BASE = "https://www.contractsfinder.service.gov.uk/Published/Notices/OCDS/Search"
PAGE_SIZE = 100


class UkContractsFinderConnector(Connector):
    source_id = "uk_contracts_finder"
    jurisdictions = ["GB"]
    data_class = DataClass.A1

    def discover(self, since: date | None = None) -> Iterator[FetchTask]:
        """Yield fetch tasks (paginated by OCDS cursor)."""
        start = since or (date.today() - timedelta(days=1))
        cursor = None
        while True:
            params: dict[str, str | int] = {
                "limit": PAGE_SIZE,
                "publishedFrom": start.isoformat(),
            }
            if cursor:
                params["cursor"] = cursor

            resp = httpx.get(API_BASE, params=params, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            releases = data.get("releases", [])
            if not releases:
                break

            yield FetchTask(
                key=f"uk_contracts_finder:{start.isoformat()}:{cursor or 'start'}",
                params={"releases": releases, "date": start.isoformat()},
            )

            # OCDS pagination extension uses cursor in the next page
            next_cursor = (
                data.get("next_page", {}).get("cursor")
                if isinstance(data.get("next_page"), dict)
                else None
            )
            # Some implementations put it in links
            if not next_cursor:
                links = data.get("links", {})
                next_cursor = (
                    links.get("next", {}).get("cursor")
                    if isinstance(links.get("next"), dict)
                    else None
                )
            if not next_cursor:
                break
            cursor = next_cursor

    def fetch(self, task: FetchTask) -> Iterator[RawArtifact]:
        """Yield raw OCDS release JSON for each release in the task."""
        releases: list[dict] = task.params["releases"]
        for release in releases:
            ocid = release.get("ocid", "")
            url = (
                f"https://www.contractsfinder.service.gov.uk/Published/OCDS/Record/{ocid}"
                if ocid
                else API_BASE
            )
            yield RawArtifact(
                payload=json.dumps(release, ensure_ascii=False).encode(),
                source_url=url,
                media_type="application/json",
            )
