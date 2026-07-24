"""Low-level security primitives: token generation, hashing, encryption."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings

TOKEN_BYTES = 32


def generate_token(nbytes: int = TOKEN_BYTES) -> str:
    """Return a random, opaque, URL-safe token."""

    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    """Hash an opaque token for at-rest storage.

    Session tokens are high-entropy random values, so a single SHA-256 pass is
    the correct primitive here (unlike passwords, which need Argon2).
    """

    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_compare(left: str, right: str) -> bool:
    return hmac.compare_digest(left, right)


def _fernet() -> Fernet:
    """Derive a Fernet key from SESSION_SECRET for provider-token encryption."""

    digest = hashlib.sha256(settings.session_secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a provider token for at-rest storage. Never returned to clients."""

    return _fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_secret(ciphertext: str) -> str | None:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


def normalize_email(email: str) -> str:
    """Normalise an email address for uniqueness comparisons."""

    return email.strip().lower()
