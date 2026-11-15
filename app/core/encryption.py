import base64
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.core.config import settings


def get_master_key() -> bytes:
    key_hex = settings.master_encryption_key
    if len(key_hex) != 64:
        raise ValueError("MASTER_ENCRYPTION_KEY must be 64 hex characters (32 bytes)")
    return bytes.fromhex(key_hex)


def encrypt_secret(plaintext: str) -> str:
    """Encrypt sensitive value with AES-256-GCM. Returns base64(nonce + ciphertext + tag)."""
    if not plaintext:
        return ""
    key = get_master_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ciphertext).decode("ascii")


def decrypt_secret(encrypted: str) -> str:
    """Decrypt value encrypted by encrypt_secret."""
    if not encrypted:
        return ""
    key = get_master_key()
    aesgcm = AESGCM(key)
    raw = base64.b64decode(encrypted)
    nonce, ciphertext = raw[:12], raw[12:]
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
