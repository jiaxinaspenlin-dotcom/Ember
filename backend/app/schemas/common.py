"""Shared Pydantic schemas."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ErrorBody(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, object] | None = None


class ErrorResponse(BaseModel):
    """The structured error envelope returned by every failing endpoint."""

    error: ErrorBody


class Page[T](BaseModel):
    items: list[T]
    total: int
    limit: int
    offset: int
    has_more: bool


class OkResponse(BaseModel):
    ok: bool = True


class UserSummary(ORMModel):
    """The only user shape ever exposed to other members.

    Email addresses are deliberately absent -- see ``docs/PERMISSIONS_AND_PRIVACY.md``.
    """

    id: uuid.UUID
    display_name: str
    avatar_url: str | None = None
    role: str | None = None


class CurrentUserOut(ORMModel):
    """The authenticated user's own account. Never includes hashes or tokens."""

    id: uuid.UUID
    email: str | None
    email_verified: bool
    display_name: str
    avatar_url: str | None
    created_at: dt.datetime
    last_login_at: dt.datetime | None = None


class CursorQuery(BaseModel):
    after_seq: int | None = Field(default=None, ge=0)
    before_seq: int | None = Field(default=None, ge=0)
    limit: int = Field(default=50, ge=1, le=100)
