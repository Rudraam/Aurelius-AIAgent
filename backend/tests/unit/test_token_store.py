from __future__ import annotations

from dataclasses import replace

import pytest
from cryptography.exceptions import InvalidTag

from app.crypto import SecretBox, generate_key_b64
from app.integrations.questrade.errors import TokenStoreConflict
from app.integrations.questrade.token_store import EncryptedFileTokenStore
from tests.unit.conftest import logged_in_store


async def test_encrypted_roundtrip_and_no_plaintext(tmp_path, clock, client_reg):
    state = await logged_in_store(clock, client_reg).load()
    path = tmp_path / "auth.enc"
    store = EncryptedFileTokenStore(path, SecretBox.from_b64(generate_key_b64()))
    saved = await store.save(replace(state, version=0), expected_version=0)
    assert saved.version == 1
    raw = path.read_bytes()
    assert b"ACCESS-1-secretvalue" not in raw and b"REFRESH-1-secretvalue" not in raw
    loaded = await store.load()
    assert loaded == saved


async def test_wrong_key_cannot_decrypt(tmp_path, clock, client_reg):
    state = await logged_in_store(clock, client_reg).load()
    path = tmp_path / "auth.enc"
    await EncryptedFileTokenStore(path, SecretBox.from_b64(generate_key_b64())).save(
        replace(state, version=0), expected_version=0
    )
    with pytest.raises(InvalidTag):
        await EncryptedFileTokenStore(path, SecretBox.from_b64(generate_key_b64())).load()


async def test_cas_conflict_writes_nothing(tmp_path, clock, client_reg):
    state = await logged_in_store(clock, client_reg).load()
    store = EncryptedFileTokenStore(tmp_path / "auth.enc", SecretBox.from_b64(generate_key_b64()))
    await store.save(replace(state, version=0), expected_version=0)
    with pytest.raises(TokenStoreConflict):
        await store.save(replace(state, tokens=None), expected_version=0)
    assert (await store.load()).tokens is not None


def test_secretbox_rejects_bad_keys():
    with pytest.raises(ValueError):
        SecretBox(b"short")
    with pytest.raises(ValueError):
        SecretBox.from_b64("not base64 !!")
    assert "redacted" in repr(SecretBox.from_b64(generate_key_b64()))
