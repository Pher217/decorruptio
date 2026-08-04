"""The one sanctioned `Entity -> dict` boundary for the MCP layer.

Every tool in `tools.py` that returns entity data goes through
`entity_summary` -- never builds its own dict from an `Entity` instance.
That is deliberate, not a style preference: `Entity.properties` is a
free-form `JSONField` that ingest connectors are *convention*-allowlisted to
keep clean (see `ch_officers._ALLOWED_OFFICER_FIELDS`,
`overseas_entities._ALLOWED_OFFICER_FIELDS`, and their docstrings for the
fields they drop -- date of birth, residential address, nationality,
occupation, country of residence, ...), not *constraint*-allowlisted by the
database. A future connector bug could write one of those fields into
`properties` and no schema would catch it. Having exactly one function that
never reads `.properties`, regardless of entity type, means that bug could
never reach an MCP caller even if it happened.
"""

from __future__ import annotations

from typing import Any

from uncorrupt.graph.models import Entity


def entity_summary(entity: Entity) -> dict[str, Any]:
    """Serialise an `Entity` for MCP output. Never reads `entity.properties`.

    Company entities additionally carry `company_number` (the join key to
    `staging.Company`, itself not personal data). Person/public_body/
    political_party/regulated_entity entities carry `role_description`
    instead -- the public-function designation (e.g. "MP for X",
    "Minister for Y") that is the whole point of an ADR-004 D1 person
    Entity existing at all, and a dedicated model column, never the
    free-form `properties` blob this function refuses to touch.
    """
    summary: dict[str, Any] = {
        "entity_id": entity.id,
        "entity_type": entity.entity_type,
        "name": entity.name,
        "registry_scheme": entity.registry_scheme,
        "registry_id": entity.registry_id,
    }
    if entity.entity_type == "company":
        summary["company_number"] = entity.company_number
    else:
        summary["role_description"] = entity.role_description
    return summary
