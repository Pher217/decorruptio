"""Ingest raw artifacts from connectors into the DuckDB staging layer.

Maps each source's native format into the unified OCDS-flattened schema:
- Ukraine Prozorro: near-OCDS JSON → tenders + awards + bids
- UK Contracts Finder: native OCDS 1.1 releases → tenders + awards
- Colombia SECOP II: Socrata rows → tenders + awards (no separate bids table)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import duckdb

from uncorrupt.connectors.base import RawArtifact


def _safe_float(v: Any) -> float | None:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _safe_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _parse_date(v: Any) -> str | None:
    if v is None or v == "":
        return None
    s = str(v)
    # ISO datetime or date
    if "T" in s:
        return s
    if len(s) == 10:
        return s + "T00:00:00"
    return s


def _ingest_ua_prozorro(conn: duckdb.DuckDBPyConnection, artifact: RawArtifact, now: str) -> None:
    """Map ProZorro tender JSON → tenders + awards + bids."""
    data = json.loads(artifact.payload)
    t = data.get("data", data)

    tender_id = t.get("id", "")
    if not tender_id:
        return

    value = t.get("value", {})
    tp = t.get("tenderPeriod", {})
    pe = t.get("procuringEntity", {})
    pe_id = pe.get("identifier", {})

    conn.execute(
        """
        INSERT OR REPLACE INTO tenders
        (source_id, tender_id, ocid, title, description, status,
         procurement_method, procurement_method_details, award_criteria,
         currency, value_amount, tender_start, tender_end,
         buyer_name, buyer_id_scheme, buyer_id, buyer_country,
         item_count, raw_json, fetched_at, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            "ua_prozorro",
            tender_id,
            t.get("tenderID"),
            t.get("title"),
            t.get("description"),
            t.get("status"),
            t.get("procurementMethod"),
            t.get("procurementMethodType"),
            t.get("awardCriteria"),
            value.get("currency"),
            _safe_float(value.get("amount")),
            _parse_date(tp.get("startDate")),
            _parse_date(tp.get("endDate")),
            pe.get("name"),
            pe_id.get("scheme"),
            pe_id.get("id"),
            pe.get("address", {}).get("countryName"),
            _safe_int(len(t.get("items", []))),
            json.dumps(t, ensure_ascii=False),
            now,
            artifact.source_url,
        ],
    )

    # Awards
    for award in t.get("awards", []):
        award_id = award.get("id", "")
        if not award_id:
            continue
        supplier = award.get("suppliers", [{}])[0] if award.get("suppliers") else {}
        sup_id = supplier.get("identifier", {})
        award_value = award.get("value", {})
        conn.execute(
            """
            INSERT OR REPLACE INTO awards
            (source_id, tender_id, award_id, supplier_name,
             supplier_id_scheme, supplier_id, currency, value_amount,
             status, award_date, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "ua_prozorro",
                tender_id,
                award_id,
                supplier.get("name"),
                sup_id.get("scheme"),
                sup_id.get("id"),
                award_value.get("currency"),
                _safe_float(award_value.get("amount")),
                award.get("status"),
                _parse_date(award.get("date")),
                json.dumps(award, ensure_ascii=False),
                now,
            ],
        )

    # Bids
    for bid in t.get("bids", []):
        bid_id = bid.get("id", "")
        if not bid_id:
            continue
        bidder = bid.get("tenderers", [{}])[0] if bid.get("tenderers") else {}
        bid_value = bid.get("value", {})
        conn.execute(
            """
            INSERT OR REPLACE INTO bids
            (source_id, tender_id, bid_id, bidder_name, bidder_id,
             currency, value_amount, status, bid_date, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "ua_prozorro",
                tender_id,
                bid_id,
                bidder.get("name"),
                bidder.get("identifier", {}).get("id"),
                bid_value.get("currency"),
                _safe_float(bid_value.get("amount")),
                bid.get("status"),
                _parse_date(bid.get("date")),
                json.dumps(bid, ensure_ascii=False),
                now,
            ],
        )


def _ingest_uk_contracts_finder(
    conn: duckdb.DuckDBPyConnection, artifact: RawArtifact, now: str
) -> None:
    """Map UK CF OCDS release → tenders + awards."""
    r = json.loads(artifact.payload)
    ocid = r.get("ocid", "")
    if not ocid:
        return

    tender = r.get("tender", {})
    tender_id = tender.get("id", ocid)
    value = tender.get("value", {})
    tp = tender.get("tenderPeriod", {})

    # Buyer from parties
    buyer_name = None
    buyer_id = None
    buyer_id_scheme = None
    for party in r.get("parties", []):
        if "buyer" in party.get("roles", []):
            buyer_name = party.get("name")
            pid = party.get("identifier", {})
            buyer_id = pid.get("id")
            buyer_id_scheme = pid.get("scheme")
            break

    conn.execute(
        """
        INSERT OR REPLACE INTO tenders
        (source_id, tender_id, ocid, title, description, status,
         procurement_method, procurement_method_details, award_criteria,
         currency, value_amount, tender_start, tender_end,
         buyer_name, buyer_id_scheme, buyer_id, buyer_country,
         item_count, raw_json, fetched_at, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            "uk_contracts_finder",
            tender_id,
            ocid,
            tender.get("title"),
            tender.get("description"),
            tender.get("status"),
            tender.get("procurementMethod"),
            tender.get("procurementMethodDetails"),
            tender.get("awardCriteria"),
            value.get("currency"),
            _safe_float(value.get("amount")),
            _parse_date(tp.get("startDate")),
            _parse_date(tp.get("endDate")),
            buyer_name,
            buyer_id_scheme,
            buyer_id,
            "GB",
            _safe_int(len(tender.get("items", []))),
            json.dumps(r, ensure_ascii=False),
            now,
            artifact.source_url,
        ],
    )

    # Awards
    for award in r.get("awards", []):
        award_id = award.get("id", "")
        if not award_id:
            continue
        supplier = award.get("suppliers", [{}])[0] if award.get("suppliers") else {}
        award_value = award.get("value", {})
        conn.execute(
            """
            INSERT OR REPLACE INTO awards
            (source_id, tender_id, award_id, supplier_name,
             supplier_id_scheme, supplier_id, currency, value_amount,
             status, award_date, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "uk_contracts_finder",
                tender_id,
                award_id,
                supplier.get("name"),
                supplier.get("identifier", {}).get("scheme"),
                supplier.get("identifier", {}).get("id"),
                award_value.get("currency"),
                _safe_float(award_value.get("amount")),
                award.get("status"),
                _parse_date(award.get("date")),
                json.dumps(award, ensure_ascii=False),
                now,
            ],
        )


def _ingest_co_secop_ii(conn: duckdb.DuckDBPyConnection, artifact: RawArtifact, now: str) -> None:
    """Map Colombia SECOP II Socrata row → tenders + awards (no separate bids)."""
    row = json.loads(artifact.payload)
    tender_id = row.get("id_del_proceso", "")
    if not tender_id:
        return

    is_adjudicado = row.get("adjudicado", "No") == "Sí"

    conn.execute(
        """
        INSERT OR REPLACE INTO tenders
        (source_id, tender_id, ocid, title, description, status,
         procurement_method, procurement_method_details, award_criteria,
         currency, value_amount, tender_start, tender_end,
         buyer_name, buyer_id_scheme, buyer_id, buyer_country,
         item_count, raw_json, fetched_at, source_url)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            "co_secop_ii",
            tender_id,
            tender_id,
            row.get("nombre_del_procedimiento"),
            row.get("descripci_n_del_procedimiento"),
            row.get("estado_del_procedimiento"),
            row.get("modalidad_de_contratacion"),
            row.get("justificaci_n_modalidad_de"),
            None,
            "COP",
            _safe_float(row.get("precio_base")),
            _parse_date(row.get("fecha_de_publicacion_del")),
            _parse_date(row.get("fecha_de_ultima_publicaci")),
            row.get("entidad"),
            "NIT",
            row.get("nit_entidad"),
            "CO",
            _safe_int(row.get("numero_de_lotes")),
            json.dumps(row, ensure_ascii=False),
            now,
            artifact.source_url,
        ],
    )

    # If adjudicated, create an award with the supplier info
    if is_adjudicado:
        award_id = f"{tender_id}-award"
        conn.execute(
            """
            INSERT OR REPLACE INTO awards
            (source_id, tender_id, award_id, supplier_name,
             supplier_id_scheme, supplier_id, currency, value_amount,
             status, award_date, raw_json, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                "co_secop_ii",
                tender_id,
                award_id,
                row.get("nombre_del_proveedor"),
                "NIT",
                row.get("nit_del_proveedor_adjudicado"),
                "COP",
                _safe_float(row.get("valor_total_adjudicacion")),
                "active" if is_adjudicado else "pending",
                _parse_date(row.get("fecha_de_ultima_publicaci")),
                json.dumps(row, ensure_ascii=False),
                now,
            ],
        )


_INGESTERS = {
    "ua_prozorro": _ingest_ua_prozorro,
    "uk_contracts_finder": _ingest_uk_contracts_finder,
    "co_secop_ii": _ingest_co_secop_ii,
}


def ingest_artifacts(
    conn: duckdb.DuckDBPyConnection,
    source_id: str,
    artifacts: list[RawArtifact],
) -> int:
    """Ingest a batch of raw artifacts from a given source. Returns count ingested."""
    ingester = _INGESTERS.get(source_id)
    if not ingester:
        raise ValueError(f"No ingester registered for source '{source_id}'")

    now = datetime.now(UTC).isoformat()
    count = 0
    for artifact in artifacts:
        try:
            ingester(conn, artifact, now)
            count += 1
        except Exception:
            continue
    return count
