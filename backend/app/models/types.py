"""Shared column helpers for models."""

from __future__ import annotations

from enum import Enum as PyEnum
from typing import Any

from sqlalchemy import Enum as SAEnum


def enum_column(enum_cls: type[PyEnum], name: str) -> SAEnum:
    """Store an enum as VARCHAR with a CHECK constraint on the permitted values.

    Using a non-native enum keeps migrations simple while still enforcing valid
    enum values at the database level.
    """

    return SAEnum(
        enum_cls,
        name=name,
        native_enum=False,
        length=40,
        validate_strings=True,
        values_callable=lambda cls: [member.value for member in cls],
    )


JSONBDict = dict[str, Any]
