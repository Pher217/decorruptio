"""Colombia SECOP II connector — real fetch from the Socrata API at datos.gov.co.

NOT OCDS-native; the normalize stage maps SECOP II fields to canonical OCDS.
Rich corruption-relevant fields: modalidad_de_contratacion, proveedores,
valor_total_adjudicacion, precio_base.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, timedelta

import httpx

from uncorrupt.connectors.base import Connector, FetchTask, RawArtifact
from uncorrupt.core.tiers import DataClass

API_BASE = "https://www.datos.gov.co/resource/p6dx-8zbt.json"
PAGE_SIZE = 1000


class CoSecopIiConnector(Connector):
    source_id = "co_secop_ii"
    jurisdictions = ["CO"]
    data_class = DataClass.A1

    def discover(self, since: date | None = None) -> Iterator[FetchTask]:
        """Yield daily fetch tasks (paginated by Socrata offset)."""
        start = since or (date.today() - timedelta(days=7))
        offset = 0
        while True:
            params: dict[str, str | int] = {
                "$limit": PAGE_SIZE,
                "$offset": offset,
                "$where": f"fecha_de_publicacion_del >= '{start.isoformat()}'",
            }
            resp = httpx.get(API_BASE, params=params, timeout=30)
            resp.raise_for_status()
            rows = resp.json()
            if not rows:
                break

            yield FetchTask(
                key=f"co_secop_ii:{start.isoformat()}:{offset}",
                params={"rows": rows, "date": start.isoformat(), "offset": offset},
            )

            if len(rows) < PAGE_SIZE:
                break
            offset += PAGE_SIZE

    def fetch(self, task: FetchTask) -> Iterator[RawArtifact]:
        """Yield raw JSON for each SECOP II process record."""
        rows: list[dict] = task.params["rows"]
        for row in rows:
            process_url = row.get("urlproceso", "")
            if isinstance(process_url, dict):
                process_url = process_url.get("url", "")
            yield RawArtifact(
                payload=json.dumps(row, ensure_ascii=False).encode(),
                source_url=process_url or API_BASE,
                media_type="application/json",
            )
