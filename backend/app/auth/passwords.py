"""Argon2 password hashing and password policy.

Plaintext passwords are never stored, never logged, and never returned.
"""

from __future__ import annotations

import re

from pwdlib import PasswordHash
from pwdlib.hashers.argon2 import Argon2Hasher

from app.core.errors import ValidationError

MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 200

_password_hash = PasswordHash((Argon2Hasher(),))

# A dummy hash used to keep verification time constant when no credential
# exists, so timing cannot be used to enumerate accounts.
_DUMMY_HASH = _password_hash.hash("ember-timing-equaliser-not-a-real-password")


def hash_password(password: str) -> str:
    return _password_hash.hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    """Verify a password in constant time.

    When ``password_hash`` is ``None`` we still perform a full Argon2
    verification against a dummy hash and return ``False``.
    """

    if password_hash is None:
        _password_hash.verify(password, _DUMMY_HASH)
        return False
    return _password_hash.verify(password, password_hash)


def verify_and_update(password: str, password_hash: str) -> tuple[bool, str | None]:
    """Verify a password and return an upgraded hash when parameters changed."""

    valid, updated = _password_hash.verify_and_update(password, password_hash)
    return valid, updated


def validate_password_strength(password: str) -> None:
    """Enforce the minimum password policy. Raises :class:`ValidationError`."""

    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters long.",
            details={"field": "password"},
        )
    if len(password) > MAX_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at most {MAX_PASSWORD_LENGTH} characters long.",
            details={"field": "password"},
        )
    if not re.search(r"[A-Za-z]", password):
        raise ValidationError(
            "Password must contain at least one letter.", details={"field": "password"}
        )
    if not re.search(r"[0-9]", password):
        raise ValidationError(
            "Password must contain at least one number.", details={"field": "password"}
        )
