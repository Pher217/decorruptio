"""Tokenized-ID linking-table interface. STUB — no personal data flows in Phase 1 (A1).

In Phase 2 (A2), this stores token -> internal-entity links only. It never stores,
returns, or exports a raw national ID.
"""

from __future__ import annotations

from typing import Protocol


class TokenLinkStore(Protocol):
    def link(self, token: str, entity_id: str) -> None: ...
    def entity_for(self, token: str) -> str | None: ...
