"""Ingest raw artifacts from connectors into the Django/PostgreSQL staging layer.

Maps each source's native format into the unified OCDS-flattened schema:
- Ukraine Prozorro: near-OCDS JSON → tenders + awards + bids
- UK Contracts Finder: native OCDS 1.1 releases → tenders + awards
- Colombia SECOP II: Socrata rows → tenders + awards (no separate bids table)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from uncorrupt.connectors.base import RawArtifact
from uncorrupt.staging.models import Award, Bid, Tender

# Per-source default currency when the source payload doesn't specify one.
DEFAULT_CURRENCY: dict[str, str] = {
    "ua_prozorro": "UAH",
    "uk_contracts_finder": "GBP",
    "co_secop_ii": "COP",
}


def _to_cents(v: Any) -> int:
    """Convert a value to integer cents using Decimal (no float round-trip)."""
    if v is None or v == "":
        return 0
    try:
        d = Decimal(str(v))
    except (InvalidOperation, ValueError, TypeError):
        return 0
    return int((d * 100).to_integral_value(rounding="ROUND_HALF_UP"))


def _safe_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return None


def _parse_dt(v: Any) -> datetime | None:
    if v is None or v == "":
        return None
    s = str(v)
    if "T" not in s and len(s) == 10:
        s = s + "T00:00:00"
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    except ValueError:
        return None


def _ingest_ua_prozorro(artifact: RawArtifact) -> None:
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

    tender, _ = Tender.objects.update_or_create(
        source_id="ua_prozorro",
        tender_id=tender_id,
        defaults={
            "ocid": t.get("tenderID"),
            "title": t.get("title"),
            "description": t.get("description"),
            "status": t.get("status"),
            "procurement_method": t.get("procurementMethod"),
            "procurement_method_details": t.get("procurementMethodType"),
            "award_criteria": t.get("awardCriteria"),
            "currency": value.get("currency") or DEFAULT_CURRENCY["ua_prozorro"],
            "value_amount_cents": _to_cents(value.get("amount")),
            "tender_start": _parse_dt(tp.get("startDate")),
            "tender_end": _parse_dt(tp.get("endDate")),
            "buyer_name": pe.get("name"),
            "buyer_id_scheme": pe_id.get("scheme"),
            "buyer_id": pe_id.get("id"),
            "buyer_country": pe.get("address", {}).get("countryName"),
            "item_count": len(t.get("items", [])),
            "raw_json": t,
            "source_url": artifact.source_url,
        },
    )

    for award in t.get("awards", []):
        award_id = award.get("id", "")
        if not award_id:
            continue
        supplier = award.get("suppliers", [{}])[0] if award.get("suppliers") else {}
        sup_id = supplier.get("identifier", {})
        award_value = award.get("value", {})
        Award.objects.update_or_create(
            source_id="ua_prozorro",
            tender_id=tender_id,
            award_id=award_id,
            defaults={
                "tender_ref": tender,
                "supplier_name": supplier.get("name"),
                "supplier_id_scheme": sup_id.get("scheme"),
                "supplier_id": sup_id.get("id"),
                "currency": award_value.get("currency") or DEFAULT_CURRENCY["ua_prozorro"],
                "value_amount_cents": _to_cents(award_value.get("amount")),
                "status": award.get("status"),
                "award_date": _parse_dt(award.get("date")),
                "raw_json": award,
            },
        )

    for bid in t.get("bids", []):
        bid_id = bid.get("id", "")
        if not bid_id:
            continue
        bidder = bid.get("tenderers", [{}])[0] if bid.get("tenderers") else {}
        bid_value = bid.get("value", {})
        Bid.objects.update_or_create(
            source_id="ua_prozorro",
            tender_id=tender_id,
            bid_id=bid_id,
            defaults={
                "tender_ref": tender,
                "bidder_name": bidder.get("name"),
                "bidder_id": bidder.get("identifier", {}).get("id"),
                "currency": bid_value.get("currency") or DEFAULT_CURRENCY["ua_prozorro"],
                "value_amount_cents": _to_cents(bid_value.get("amount")),
                "status": bid.get("status"),
                "bid_date": _parse_dt(bid.get("date")),
                "raw_json": bid,
            },
        )


def _ingest_uk_contracts_finder(artifact: RawArtifact) -> None:
    """Map UK CF OCDS release → tenders + awards."""
    r = json.loads(artifact.payload)
    ocid = r.get("ocid", "")
    if not ocid:
        return

    tender = r.get("tender", {})
    tender_id = tender.get("id", ocid)
    value = tender.get("value", {})
    tp = tender.get("tenderPeriod", {})

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

    tender_obj, _ = Tender.objects.update_or_create(
        source_id="uk_contracts_finder",
        tender_id=tender_id,
        defaults={
            "ocid": ocid,
            "title": tender.get("title"),
            "description": tender.get("description"),
            "status": tender.get("status"),
            "procurement_method": tender.get("procurementMethod"),
            "procurement_method_details": tender.get("procurementMethodDetails"),
            "award_criteria": tender.get("awardCriteria"),
            "currency": value.get("currency") or DEFAULT_CURRENCY["uk_contracts_finder"],
            "value_amount_cents": _to_cents(value.get("amount")),
            "tender_start": _parse_dt(tp.get("startDate")),
            "tender_end": _parse_dt(tp.get("endDate")),
            "buyer_name": buyer_name,
            "buyer_id_scheme": buyer_id_scheme,
            "buyer_id": buyer_id,
            "buyer_country": "GB",
            "item_count": len(tender.get("items", [])),
            "raw_json": r,
            "source_url": artifact.source_url,
        },
    )

    # Build party lookup for supplier identifier cross-reference.
    # OCDS convention: identifiers live in parties[], not awards[].suppliers[].
    party_by_id: dict[str, dict] = {}
    party_by_name: dict[str, dict] = {}
    for p in r.get("parties", []):
        if p.get("id"):
            party_by_id[p["id"]] = p
        if p.get("name"):
            party_by_name[p["name"]] = p

    for award in r.get("awards", []):
        award_id = award.get("id", "")
        if not award_id:
            continue
        award_value = award.get("value", {})
        suppliers_list = award.get("suppliers", [])

        for sup_idx, supplier in enumerate(suppliers_list):
            # Cross-reference parties[] by supplier id, falling back to name.
            sup_id = supplier.get("id", "")
            sup_name = supplier.get("name", "")
            supplier_id_scheme = supplier.get("identifier", {}).get("scheme")
            supplier_id = supplier.get("identifier", {}).get("id")

            if not supplier_id and sup_id:
                party = party_by_id.get(sup_id)
                if party:
                    pid = party.get("identifier", {})
                    supplier_id_scheme = pid.get("scheme")
                    supplier_id = pid.get("id")

            if not supplier_id and sup_name:
                party = party_by_name.get(sup_name)
                if party:
                    pid = party.get("identifier", {})
                    supplier_id_scheme = pid.get("scheme")
                    supplier_id = pid.get("id")

            # Make award_id unique per supplier to satisfy the UNIQUE constraint.
            unique_award_id = f"{award_id}:{sup_idx}" if len(suppliers_list) > 1 else award_id

            # D2: multi-supplier awards carry a framework ceiling, not per-supplier money.
            # Null the value when N>1 so indicators don't aggregate a ceiling as spend.
            per_supplier_value = (
                _to_cents(award_value.get("amount")) if len(suppliers_list) == 1 else None
            )

            Award.objects.update_or_create(
                source_id="uk_contracts_finder",
                tender_id=tender_id,
                award_id=unique_award_id,
                defaults={
                    "tender_ref": tender_obj,
                    "supplier_name": sup_name,
                    "supplier_id_scheme": supplier_id_scheme,
                    "supplier_id": supplier_id,
                    "currency": award_value.get("currency")
                    or DEFAULT_CURRENCY["uk_contracts_finder"],
                    "value_amount_cents": per_supplier_value,
                    "status": award.get("status"),
                    "award_date": _parse_dt(award.get("date")),
                    "raw_json": award,
                },
            )


def _ingest_co_secop_ii(artifact: RawArtifact) -> None:
    """Map Colombia SECOP II Socrata row → tenders + awards (no separate bids)."""
    row = json.loads(artifact.payload)
    tender_id = row.get("id_del_proceso", "")
    if not tender_id:
        return

    is_adjudicado = row.get("adjudicado", "No") in ("Sí", "Si", "si", "sí")

    tender, _ = Tender.objects.update_or_create(
        source_id="co_secop_ii",
        tender_id=tender_id,
        defaults={
            "ocid": tender_id,
            "title": row.get("nombre_del_procedimiento"),
            "description": row.get("descripci_n_del_procedimiento"),
            "status": row.get("estado_del_procedimiento"),
            "procurement_method": row.get("modalidad_de_contratacion"),
            "procurement_method_details": row.get("justificaci_n_modalidad_de"),
            "award_criteria": None,
            "currency": "COP",
            "value_amount_cents": _to_cents(row.get("precio_base")),
            "tender_start": _parse_dt(row.get("fecha_de_publicacion_del")),
            "tender_end": _parse_dt(row.get("fecha_de_ultima_publicaci")),
            "buyer_name": row.get("entidad"),
            "buyer_id_scheme": "NIT",
            "buyer_id": row.get("nit_entidad"),
            "buyer_country": "CO",
            "item_count": _safe_int(row.get("numero_de_lotes")),
            "raw_json": row,
            "source_url": artifact.source_url,
        },
    )

    if is_adjudicado:
        award_id = row.get("id_adjudicacion") or f"{tender_id}-award"
        Award.objects.update_or_create(
            source_id="co_secop_ii",
            tender_id=tender_id,
            award_id=award_id,
            defaults={
                "tender_ref": tender,
                "supplier_name": row.get("nombre_del_proveedor"),
                "supplier_id_scheme": "NIT",
                "supplier_id": row.get("nit_del_proveedor_adjudicado"),
                "currency": "COP",
                "value_amount_cents": _to_cents(row.get("valor_total_adjudicacion")),
                "status": "active",
                "award_date": _parse_dt(row.get("fecha_adjudicacion")),
                "raw_json": row,
            },
        )


_INGESTERS = {
    "ua_prozorro": _ingest_ua_prozorro,
    "uk_contracts_finder": _ingest_uk_contracts_finder,
    "co_secop_ii": _ingest_co_secop_ii,
}


def ingest_artifacts(source_id: str, artifacts: list[RawArtifact]) -> int:
    """Ingest a batch of raw artifacts from a given source. Returns count ingested."""
    ingester = _INGESTERS.get(source_id)
    if not ingester:
        raise ValueError(f"No ingester registered for source '{source_id}'")

    count = 0
    failed = 0
    for artifact in artifacts:
        try:
            ingester(artifact)
            count += 1
        except Exception:
            failed += 1
            continue
    print(f"  [{source_id}] ingested {count}, failed {failed}")
    return count
