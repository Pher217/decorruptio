"""Immutable, content-addressed raw zone (the evidence). Write-once enforced.

Never mutate raw — this is what makes findings reproducible (ADR-000 G6).
"""

from __future__ import annotations

from hashlib import sha256


def content_hash(payload: bytes) -> str:
    return sha256(payload).hexdigest()
