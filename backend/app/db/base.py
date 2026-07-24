"""Declarative base and shared column types."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any, cast

from sqlalchemy import DateTime, MetaData, func
from sqlalchemy.engine import CursorResult, Result
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Base class for all ORM models."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)

    type_annotation_map = {
        dt.datetime: DateTime(timezone=True),
    }

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        pk = getattr(self, "id", None)
        return f"<{type(self).__name__} id={pk}>"


def uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    """Adds created_at / updated_at columns maintained by the database."""

    created_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), nullable=False, index=True
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        server_default=func.now(), onupdate=func.now(), nullable=False
    )


def utcnow() -> dt.datetime:
    """Timezone-aware "now" used everywhere in the application layer."""

    return dt.datetime.now(dt.UTC)


JSONDict = dict[str, Any]


def rows_affected(result: Result[Any]) -> int:
    """Number of rows an UPDATE/DELETE touched.

    ``Session.execute`` is typed as returning ``Result``; DML always yields a
    ``CursorResult``, which is where ``rowcount`` lives.
    """

    return int(cast(CursorResult[Any], result).rowcount or 0)
