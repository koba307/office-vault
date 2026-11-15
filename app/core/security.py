"""Signing helpers for callback data and one-time invite tokens.

Callback payloads are signed with HMAC-SHA256 so a forged ``callback_data``
cannot trigger actions, and invite tokens are stored in the database only as
SHA-256 hashes. Standard library only: no external crypto dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# Domain separation: the callback key must not equal any other key derived from
# the master secret.
_CALLBACK_KEY_CONTEXT = b"office-vault:callback:v1"

# 32 random bytes give a 43-character URL-safe token (~256 bit entropy).
_INVITE_TOKEN_BYTES = 32


def _get_signing_key() -> bytes:
    """Derive a dedicated 32-byte HMAC key from the configured master secret."""
    # Локальный импорт: модуль не требует настроек при простом импорте.
    from app.core.config import settings

    master_key = settings.master_encryption_key
    if not master_key:
        raise RuntimeError("MASTER_ENCRYPTION_KEY is not configured")

    return hashlib.sha256(_CALLBACK_KEY_CONTEXT + master_key.encode("utf-8")).digest()


def generate_callback_token(payload: str) -> str:
    """Return a hex HMAC-SHA256 signature for the callback payload."""
    if not isinstance(payload, str):
        raise TypeError("payload must be a string")

    signature = hmac.new(_get_signing_key(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return signature


def verify_callback_token(payload: str, token: str) -> bool:
    """Constant-time verification of a callback signature."""
    if not isinstance(payload, str) or not isinstance(token, str):
        return False
    if not token:
        return False

    expected = generate_callback_token(payload)
    return hmac.compare_digest(expected, token.strip().lower())


def generate_invite_token() -> str:
    """Generate a cryptographically strong URL-safe invite token."""
    return secrets.token_urlsafe(_INVITE_TOKEN_BYTES)


def hash_invite_token(token: str) -> str:
    """Return the SHA-256 hex digest stored in the database instead of the token."""
    if not isinstance(token, str) or not token:
        raise ValueError("invite token must be a non-empty string")

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_invite_token(token: str, stored_hash: str) -> bool:
    """Compare a presented invite token with its stored hash in constant time."""
    if not isinstance(token, str) or not token:
        return False
    if not isinstance(stored_hash, str) or not stored_hash:
        return False

    return hmac.compare_digest(hash_invite_token(token), stored_hash.strip().lower())
