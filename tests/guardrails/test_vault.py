"""The tokenized-ID vault refuses to operate without a key, and never returns a raw
ID; tokens are keyed-HMAC and stable (ADR-000 G3)."""

import pytest

from uncorrupt.core.errors import VaultError
from uncorrupt.vault.tokenizer import tokenize


def test_refuses_without_key(monkeypatch):
    monkeypatch.delenv("UNCORRUPT_VAULT_HMAC_KEY", raising=False)
    with pytest.raises(VaultError):
        tokenize("123", id_type="cpf")


def test_token_is_stable_and_not_the_raw_id(monkeypatch):
    monkeypatch.setenv("UNCORRUPT_VAULT_HMAC_KEY", "test-key")
    t1 = tokenize("11144477735", id_type="cpf")
    t2 = tokenize("11144477735", id_type="cpf")
    assert t1 == t2
    assert "11144477735" not in t1
    assert len(t1) == 64  # sha256 hex


def test_id_type_namespaced(monkeypatch):
    monkeypatch.setenv("UNCORRUPT_VAULT_HMAC_KEY", "test-key")
    assert tokenize("123", id_type="cpf") != tokenize("123", id_type="nif")
