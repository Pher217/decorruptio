"""Global entity keys.

The canonical company key is the composite (jurisdiction, registry_number).
GLEIF LEI is an *enrichment overlay*, never the primary key: most companies —
especially the SMEs that win municipal contracts — have no LEI (ADR-001 D4).

National *personal* IDs are never used as cross-border keys; they live only in
the tokenized-ID vault (see uncorrupt.vault).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompanyKey:
    jurisdiction: str        # ISO 3166 alpha-2
    registry_number: str     # national company-registry identifier
    lei: str | None = None   # GLEIF LEI, enrichment overlay only

    def __str__(self) -> str:
        return f"{self.jurisdiction}:{self.registry_number}"
