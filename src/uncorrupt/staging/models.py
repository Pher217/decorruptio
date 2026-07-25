"""Django ORM models for the staging layer.

Replaces the DuckDB schema (ADR-002 D2 amendment).
All monetary values stored as integer cents — never floats (Architecture Principles).
"""

from __future__ import annotations

from django.db import models


class Tender(models.Model):
    """A procurement process (tender). One row per source-native tender ID."""

    source_id = models.CharField(max_length=50, db_index=True)
    tender_id = models.CharField(max_length=200)
    ocid = models.CharField(max_length=200, null=True, blank=True)
    title = models.TextField(null=True, blank=True)
    description = models.TextField(null=True, blank=True)
    status = models.CharField(max_length=50, null=True, blank=True)
    procurement_method = models.CharField(max_length=50, null=True, blank=True)
    procurement_method_details = models.CharField(max_length=100, null=True, blank=True)
    award_criteria = models.CharField(max_length=50, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    value_amount_cents = models.BigIntegerField(default=0)
    tender_start = models.DateTimeField(null=True, blank=True)
    tender_end = models.DateTimeField(null=True, blank=True)
    buyer_name = models.CharField(max_length=500, null=True, blank=True)
    buyer_id_scheme = models.CharField(max_length=50, null=True, blank=True)
    buyer_id = models.CharField(max_length=200, null=True, blank=True)
    buyer_country = models.CharField(max_length=10, null=True, blank=True)
    item_count = models.IntegerField(null=True, blank=True)
    raw_json = models.JSONField(default=dict)
    source_url = models.URLField(max_length=1000)
    fetched_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("source_id", "tender_id")]
        indexes = [
            models.Index(fields=["source_id", "buyer_name"]),
            models.Index(fields=["procurement_method"]),
        ]

    def __str__(self) -> str:
        return f"{self.source_id}:{self.tender_id} ({self.title or 'untitled'})"


class Award(models.Model):
    """A contract award within a tender."""

    source_id = models.CharField(max_length=50, db_index=True)
    tender_id = models.CharField(max_length=200)
    award_id = models.CharField(max_length=200)
    supplier_name = models.CharField(max_length=500, null=True, blank=True)
    supplier_id_scheme = models.CharField(max_length=50, null=True, blank=True)
    supplier_id = models.CharField(max_length=200, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    value_amount_cents = models.BigIntegerField(default=0)
    status = models.CharField(max_length=50, null=True, blank=True)
    award_date = models.DateTimeField(null=True, blank=True)
    raw_json = models.JSONField(default=dict)
    fetched_at = models.DateTimeField(auto_now_add=True)

    tender_ref = models.ForeignKey(
        Tender,
        on_delete=models.CASCADE,
        related_name="awards",
        null=True,
        blank=True,
    )

    class Meta:
        unique_together = [("source_id", "tender_id", "award_id")]
        indexes = [
            models.Index(fields=["source_id", "supplier_name"]),
        ]

    def __str__(self) -> str:
        return f"{self.source_id}:{self.tender_id}:{self.award_id}"


class Bid(models.Model):
    """A bid/proposal submitted for a tender."""

    source_id = models.CharField(max_length=50, db_index=True)
    tender_id = models.CharField(max_length=200)
    bid_id = models.CharField(max_length=200)
    bidder_name = models.CharField(max_length=500, null=True, blank=True)
    bidder_id = models.CharField(max_length=200, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    value_amount_cents = models.BigIntegerField(null=True, blank=True)
    status = models.CharField(max_length=50, null=True, blank=True)
    bid_date = models.DateTimeField(null=True, blank=True)
    raw_json = models.JSONField(default=dict)
    fetched_at = models.DateTimeField(auto_now_add=True)

    tender_ref = models.ForeignKey(
        Tender,
        on_delete=models.CASCADE,
        related_name="bids",
        null=True,
        blank=True,
    )

    class Meta:
        unique_together = [("source_id", "tender_id", "bid_id")]

    def __str__(self) -> str:
        return f"{self.source_id}:{self.tender_id}:{self.bid_id}"


class Flag(models.Model):
    """An anomaly flag produced by an indicator. Carries provenance for reproducibility."""

    indicator_id = models.CharField(max_length=50, db_index=True)
    subject_ref = models.CharField(max_length=500)
    as_of = models.DateField()
    explanation = models.TextField()
    evidence_json = models.JSONField(default=list)
    stamp_json = models.JSONField(default=dict)
    tender_ref = models.ForeignKey(
        Tender,
        on_delete=models.SET_NULL,
        related_name="flags",
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    REVIEW_CHOICES = [
        ("pending", "Pending"),
        ("confirmed", "Confirmed"),
        ("rejected", "Rejected"),
        ("escalated", "Escalated"),
    ]
    review_status = models.CharField(
        max_length=20,
        choices=REVIEW_CHOICES,
        default="pending",
        db_index=True,
    )

    class Meta:
        indexes = [
            models.Index(fields=["indicator_id", "review_status"]),
            models.Index(fields=["as_of"]),
        ]

    def __str__(self) -> str:
        return f"{self.indicator_id}:{self.subject_ref} ({self.review_status})"


class Company(models.Model):
    """A company from the Companies House "Basic Company Data" bulk CSV.

    Company-level data only — no officers, no PSC, no personal data (scope boundary).
    """

    company_number = models.CharField(max_length=20, primary_key=True)
    company_name = models.CharField(max_length=500, db_index=True)
    company_status = models.CharField(max_length=50, null=True, blank=True)
    incorporation_date = models.DateField(null=True, blank=True)
    accounts_category = models.CharField(max_length=100, null=True, blank=True)
    accounts_last_made_up_date = models.DateField(null=True, blank=True)
    sic_codes = models.TextField(null=True, blank=True)
    registered_address = models.TextField(null=True, blank=True)
    # Normalised name for tier-2 matching (uppercase, stripped whitespace)
    normalised_name = models.CharField(max_length=500, db_index=True, null=True, blank=True)
    # Which CH bulk snapshot this came from
    bulk_snapshot_date = models.DateField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=["normalised_name"]),
            models.Index(fields=["company_name"]),
        ]

    def __str__(self) -> str:
        return f"{self.company_number}: {self.company_name}"


class SupplierResolution(models.Model):
    """Resolution of a supplier (from Awards) to a Companies House company.

    Tier 1: identifier match (supplier_id with scheme GB-COH → company_number). Confidence 1.0.
    Tier 2: exact name match (uniqueness-guarded — only when exactly one company has that name).
    Tier 3: normalised name match — deferred (fuzzy), not yet built.
    """

    MATCH_METHODS = [
        ("identifier", "Identifier (GB-COH)"),
        ("exact_name", "Exact name"),
        ("normalised_name", "Normalised name (deferred)"),
    ]

    source_id = models.CharField(max_length=50, db_index=True)
    supplier_name = models.CharField(max_length=500)
    supplier_id_scheme = models.CharField(max_length=50, null=True, blank=True)
    supplier_id = models.CharField(max_length=200, null=True, blank=True)

    company = models.ForeignKey(
        Company,
        on_delete=models.CASCADE,
        related_name="resolutions",
        null=True,
        blank=True,
    )
    company_number = models.CharField(max_length=20, null=True, blank=True, db_index=True)
    match_confidence = models.FloatField(default=0.0)
    match_method = models.CharField(max_length=20, choices=MATCH_METHODS, null=True, blank=True)
    # For non-identifier matches, how the name was normalised
    normalisation_note = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("source_id", "supplier_name")]
        indexes = [
            models.Index(fields=["source_id", "match_method"]),
            models.Index(fields=["company_number"]),
        ]

    def __str__(self) -> str:
        return (
            f"{self.source_id}:{self.supplier_name} → {self.company_number} ({self.match_method})"
        )
