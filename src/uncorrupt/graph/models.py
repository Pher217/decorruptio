"""FtM-shaped graph models for relationship recovery.

Entities are companies, public officials, or public bodies. Edges are typed,
dated relationships (donation, officer_of, referred_to_lane, declared_interest).
Every edge carries valid_from / valid_to and a source citation — temporal
correctness is the core deliverable of Phase 1, not a nicety.

Resolution by registry ID wherever possible, never by person name string
(ADR-004 D2). Company-level + public-function officials only — no PSC, no
private-individual profiling (ADR-004 D1).
"""

from __future__ import annotations

from django.db import models


class Entity(models.Model):
    """A company, public official, or public body.

    Companies link to the existing staging.Company by company_number.
    Public officials are identified by their public role (MP, Lord, minister)
    and registry ID where available — never resolved by name string alone.
    """

    ENTITY_TYPES = [
        ("company", "Company"),
        ("person", "Person (public official)"),
        ("public_body", "Public body / government organization"),
        ("political_party", "Political party"),
        (
            "regulated_entity",
            "Other EC-regulated entity (members association, third-party campaigner, etc.)",
        ),
    ]

    entity_type = models.CharField(max_length=20, choices=ENTITY_TYPES, db_index=True)
    name = models.CharField(max_length=500, db_index=True)

    # Registry identification — the join key (ADR-004 D2)
    registry_scheme = models.CharField(max_length=50, null=True, blank=True)
    registry_id = models.CharField(max_length=50, null=True, blank=True, db_index=True)

    # Link to staging.Company when entity_type == "company"
    company_number = models.CharField(max_length=20, null=True, blank=True, db_index=True)

    # Role / title for public officials (e.g. "MP for X", "Minister for Y")
    role_description = models.CharField(max_length=500, null=True, blank=True)

    # Additional context
    properties = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["entity_type", "registry_scheme", "registry_id"]),
            models.Index(fields=["company_number"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["registry_scheme", "registry_id"],
                name="unique_registry_id",
                condition=models.Q(registry_id__isnull=False),
            ),
        ]

    def __str__(self) -> str:
        return f"{self.entity_type}: {self.name}"


class Alias(models.Model):
    """An alternative name for an entity (trading name, former name, alias).

    An alias is a provenance-bearing claim exactly like an edge: "X is also
    known as Y" causes entity merges when wrong, which is this project's most
    serious failure mode. Every alias must therefore carry a source citation.
    """

    entity = models.ForeignKey(
        Entity,
        on_delete=models.CASCADE,
        related_name="aliases",
    )
    name = models.CharField(max_length=500, db_index=True)
    alias_type = models.CharField(
        max_length=30,
        null=True,
        blank=True,
        help_text="trading_as, former_name, known_as, etc.",
    )
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)

    # Source citation — mirrors Edge (an alias is a claim, not a fact)
    source_name = models.CharField(
        max_length=200,
        help_text="Companies House, Electoral Commission, Parliament Register, etc.",
    )
    source_url = models.URLField(max_length=1000, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["name"]),
        ]

    def __str__(self) -> str:
        return f"{self.entity.name} → {self.name} ({self.alias_type or 'alias'})"


class Edge(models.Model):
    """A typed, dated relationship between two entities.

    Every edge carries valid_from / valid_to and a source citation. A
    relationship that began after an award must never be presented as
    pre-existing — temporal correctness is the core of Phase 1.
    """

    EDGE_TYPES = [
        ("donation", "Political donation"),
        ("officer_of", "Officer / director of company"),
        ("referred_to_lane", "Referred supplier to VIP lane"),
        ("declared_interest", "Declared financial interest"),
        ("supplier_of", "Supplier of public contract"),
        ("associate_of", "Professional or personal association"),
    ]

    edge_type = models.CharField(max_length=30, choices=EDGE_TYPES, db_index=True)

    source_entity = models.ForeignKey(
        Entity,
        on_delete=models.CASCADE,
        related_name="outgoing_edges",
    )
    target_entity = models.ForeignKey(
        Entity,
        on_delete=models.CASCADE,
        related_name="incoming_edges",
    )

    # Temporal provenance — the core of this phase
    valid_from = models.DateField(null=True, blank=True)
    valid_to = models.DateField(null=True, blank=True)

    # Source citation (embedded — 52 referrals don't justify a separate table)
    source_name = models.CharField(
        max_length=200,
        help_text="Electoral Commission, Parliament Register, Companies House, DHSC, etc.",
    )
    source_url = models.URLField(max_length=1000, null=True, blank=True)
    source_reference = models.CharField(
        max_length=200,
        null=True,
        blank=True,
        help_text="Source-native ID (e.g. EC donation ref, Parliament interest ID)",
    )

    # Match provenance — how source_entity/target_entity were resolved. An
    # edge built from tier-2 name matching is a weaker claim than one built
    # from a registry-ID match and must say so (PR #6).
    match_confidence = models.FloatField(default=1.0)
    match_method = models.CharField(max_length=30, default="identifier")

    # Monetary values MUST use amount_cents/currency, never properties — no
    # floats for money, and no untyped JSON for a fact this load-bearing.
    amount_cents = models.BigIntegerField(null=True, blank=True)
    currency = models.CharField(max_length=3, null=True, blank=True)

    # Edge-specific metadata (officer role, etc.) — NOT for monetary values.
    properties = models.JSONField(default=dict)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["edge_type", "valid_from"]),
            models.Index(fields=["source_entity", "target_entity", "edge_type"]),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(source_name=""),
                name="edge_source_name_not_empty",
            ),
            models.CheckConstraint(
                condition=models.Q(match_confidence__gte=0.0) & models.Q(match_confidence__lte=1.0),
                name="edge_match_confidence_range",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_from__isnull=True)
                | models.Q(valid_to__gte=models.F("valid_from")),
                name="edge_valid_to_after_valid_from",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"[{self.edge_type}] {self.source_entity.name} → "
            f"{self.target_entity.name} ({self.valid_from or '?'})"
        )
