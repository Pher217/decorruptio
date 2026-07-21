"""Keyed-HMAC tokenizer for national IDs (ADR-000 G3).

Tokenization is NOT anonymization: a token derived from a CPF/NIF remains personal
data requiring a legal basis. The point is to avoid persisting or displaying the
raw ID, and to make the token brute-force-resistant (keyed HMAC, not a bare hash).

There is deliberately NO API that persists or returns a raw national ID.
"""

from __future__ import annotations

import hmac
import os
from hashlib import sha256

from uncorrupt.core.errors import VaultError

_ENV_KEY = "UNCORRUPT_VAULT_HMAC_KEY"


def _key() -> bytes:
    raw = os.environ.get(_ENV_KEY, "")
    if not raw:
        raise VaultError(
            f"{_ENV_KEY} is not set; the tokenized-ID vault refuses to operate"
        )
    return raw.encode("utf-8")


def tokenize(national_id: str, *, id_type: str) -> str:
    """Return a stable, non-reversible token for a national ID.

    `id_type` (e.g. 'cpf', 'nif') is mixed in so the same digits under different
    schemes don't collide. The raw id is never stored by this function.
    """
    if not national_id:
        raise VaultError("refusing to tokenize an empty national id")
    msg = f"{id_type}:{national_id}".encode()
    return hmac.new(_key(), msg, sha256).hexdigest()
