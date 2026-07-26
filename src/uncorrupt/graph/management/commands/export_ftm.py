"""Export the relationship graph to FollowTheMoney JSON entities.

OpenAleph migration insurance (ADR-005 D4): if the project ever needs to
migrate to OpenAleph, this command produces FtM-shaped JSON that Aleph's
ingest framework can consume directly. Until then, it serves as a
structural compatibility check — the graph models are FtM-shaped but do
not take the FtM library as a dependency.

Output format: one JSON object per line (JSON Lines / .jsonl), each a
FollowTheMoney entity proxy dict with ``id``, ``schema``, ``properties``.

Mapping:
    Entity (company)          → FtM Company
    Entity (person)           → FtM Person
    Entity (public_body)      → FtM PublicBody
    Entity (political_party)  → FtM Organization
    Entity (regulated_entity) → FtM Organization
    Edge (donation)           → FtM Payment
    Edge (officer_of)         → FtM Directorship
    Edge (declared_interest)  → FtM Thing (titled)
    Edge (referred_to_lane)   → FtM Thing (titled)
    Edge (supplier_of)        → FtM Contract
    Edge (associate_of)       → FtM Thing (titled)
    Attestation               → embedded as provenance on the edge's FtM entity
"""

from __future__ import annotations

import json
from pathlib import Path

from django.core.management.base import BaseCommand
from django.db import transaction

from uncorrupt.graph.models import Edge, Entity


def _entity_to_ftm(entity: Entity) -> dict:
    """Convert a graph Entity to a FollowTheMoney entity proxy dict."""
    schema_map = {
        "company": "Company",
        "person": "Person",
        "public_body": "PublicBody",
        "political_party": "Organization",
        "regulated_entity": "Organization",
    }
    schema = schema_map.get(entity.entity_type, "Thing")

    props: dict[str, list[str]] = {
        "name": [entity.name],
    }

    if entity.registry_scheme and entity.registry_id:
        props["registerId"] = [f"{entity.registry_scheme}:{entity.registry_id}"]

    if entity.company_number:
        props["registrationNumber"] = [entity.company_number]

    if entity.role_description:
        props["title"] = [entity.role_description]

    if entity.properties:
        for key, value in entity.properties.items():
            if isinstance(value, str):
                props.setdefault(key, []).append(value)

    return {
        "id": f"uncorrupt:entity:{entity.pk}",
        "schema": schema,
        "properties": props,
    }


def _edge_to_ftm(edge: Edge) -> dict:
    """Convert a graph Edge to a FollowTheMoney entity proxy dict."""
    schema_map = {
        "donation": "Payment",
        "officer_of": "Directorship",
        "declared_interest": "Thing",
        "referred_to_lane": "Thing",
        "supplier_of": "Contract",
        "associate_of": "Thing",
    }
    schema = schema_map.get(edge.edge_type, "Thing")

    props: dict[str, list[str]] = {
        "schema": [schema],
    }

    # FtM relationships use entity references
    props["starter"] = [f"uncorrupt:entity:{edge.source_entity_id}"]
    props["ender"] = [f"uncorrupt:entity:{edge.target_entity_id}"]

    if edge.valid_from:
        props["startDate"] = [edge.valid_from.isoformat()]
    if edge.valid_to:
        props["endDate"] = [edge.valid_to.isoformat()]

    if edge.amount_cents is not None:
        # FtM uses string amounts with currency
        amount_str = f"{edge.amount_cents / 100:.2f}"
        if edge.currency:
            amount_str = f"{amount_str} {edge.currency}"
        props["amount"] = [amount_str]

    if edge.properties:
        for key, value in edge.properties.items():
            if isinstance(value, str):
                props.setdefault(key, []).append(value)

    # Embed attestations as provenance
    attestations = edge.attestations.all()
    if attestations:
        prov_list = []
        for att in attestations:
            prov: dict[str, str] = {
                "source": att.source_name,
            }
            if att.source_url:
                prov["url"] = att.source_url
            if att.source_reference:
                prov["reference"] = att.source_reference
            if att.observed_at:
                prov["observedAt"] = att.observed_at.isoformat()
            if att.snapshot_ref:
                prov["snapshotRef"] = att.snapshot_ref
            prov_list.append(json.dumps(prov))
        props["provenance"] = prov_list

    return {
        "id": f"uncorrupt:edge:{edge.pk}",
        "schema": schema,
        "properties": props,
    }


class Command(BaseCommand):
    help = "Export the relationship graph to FollowTheMoney JSON Lines format."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output",
            type=str,
            default="experiments/ftm_export.jsonl",
            help="Output file path (default: experiments/ftm_export.jsonl)",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        output_path = Path(options["output"])
        output_path.parent.mkdir(parents=True, exist_ok=True)

        entity_count = 0
        edge_count = 0

        with open(output_path, "w", encoding="utf-8") as f:
            for entity in Entity.objects.iterator():
                ftm = _entity_to_ftm(entity)
                f.write(json.dumps(ftm) + "\n")
                entity_count += 1

            for edge in Edge.objects.select_related("source_entity", "target_entity").iterator():
                ftm = _edge_to_ftm(edge)
                f.write(json.dumps(ftm) + "\n")
                edge_count += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"Exported {entity_count} entities + {edge_count} edges → {output_path}"
            )
        )
