"""AES-GCM envelope for secrets at rest. The key comes from AURELIUS_ENCRYPTION_KEY (base64, 32 bytes)."""

from __future__ import annotations

import base64
import binascii
import secrets

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12
_VERSION = b"\x01"


class SecretBox:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError("encryption key must be exactly 32 bytes")
        self._aead = AESGCM(key)

    @classmethod
    def from_b64(cls, value: str) -> SecretBox:
        try:
            key = base64.b64decode(value, validate=True)
        except binascii.Error as e:
            raise ValueError("encryption key is not valid base64") from e
        return cls(key)

    def __repr__(self) -> str:
        return "SecretBox(<redacted>)"

    def encrypt(self, plaintext: bytes, aad: bytes) -> bytes:
        nonce = secrets.token_bytes(_NONCE_LEN)
        return _VERSION + nonce + self._aead.encrypt(nonce, plaintext, aad)

    def decrypt(self, blob: bytes, aad: bytes) -> bytes:
        if blob[:1] != _VERSION:
            raise ValueError("unknown ciphertext version")
        nonce, ct = blob[1 : 1 + _NONCE_LEN], blob[1 + _NONCE_LEN :]
        return self._aead.decrypt(nonce, ct, aad)


def generate_key_b64() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode()
