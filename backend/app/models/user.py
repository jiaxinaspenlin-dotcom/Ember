"""Identity and credentials (global, cross-cohort).

A ``User`` is a single global identity: one email/password/GitHub login. What a
person *is* inside a particular cohort -- their role, profile, skills, status --
lives on ``CohortMembership`` (see ``app/models/cohort.py``), because a person
can belong to several cohorts and be a different thing in each.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import EmailTokenPurpose
from app.db.base import Base, TimestampMixin
from app.models.types import enum_column

if TYPE_CHECKING:
    from app.models.cohort import CohortMembership


class User(Base, TimestampMixin):
    """A person's global identity.

    ``email`` is nullable because GitHub accounts may not expose an address;
    when present it is stored normalised and is unique across the installation.
    A user has no role here -- roles are per cohort.
    """

    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("email", name="uq_users_email"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    email_verified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_login_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # Drives online presence: refreshed (throttled) whenever the user makes a
    # request. Null until their first activity.
    last_active_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    password_credential: Mapped[PasswordCredential | None] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    oauth_accounts: Mapped[list[OAuthAccount]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    cohort_memberships: Mapped[list[CohortMembership]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class OAuthAccount(Base, TimestampMixin):
    """A third-party identity linked to a :class:`User`."""

    __tablename__ = "oauth_accounts"
    __table_args__ = (
        UniqueConstraint(
            "provider", "provider_account_id", name="uq_oauth_accounts_provider_account"
        ),
        Index("ix_oauth_accounts_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_account_id: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_username: Mapped[str | None] = mapped_column(String(120))
    provider_email: Mapped[str | None] = mapped_column(String(320))
    scopes: Mapped[str | None] = mapped_column(String(255))
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)

    user: Mapped[User] = relationship(back_populates="oauth_accounts")


class PasswordCredential(Base, TimestampMixin):
    """Argon2 password hash for email/password sign-in."""

    __tablename__ = "password_credentials"
    __table_args__ = (UniqueConstraint("user_id", name="uq_password_credentials_user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    user: Mapped[User] = relationship(back_populates="password_credential")


class UserSession(Base):
    """A server-side session. Only the *hash* of the token is stored.

    ``active_cohort_id`` remembers which cohort the browser is currently working
    in, so navigation stays inside one tenant until the user switches.
    """

    __tablename__ = "sessions"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
        Index("ix_sessions_user_id_expires_at", "user_id", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    active_cohort_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("cohorts.id", ondelete="SET NULL")
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    user_agent: Mapped[str | None] = mapped_column(String(255))
    ip_address: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship()


class OAuthState(Base):
    """Server-side OAuth state, so the CSRF check never depends on app memory."""

    __tablename__ = "oauth_states"
    __table_args__ = (UniqueConstraint("state", name="uq_oauth_states_state"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    state: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False, default="github")
    redirect_to: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class LoginAttempt(Base):
    """Persisted login attempts backing the rate limiter."""

    __tablename__ = "login_attempts"
    __table_args__ = (
        Index("ix_login_attempts_identifier_created_at", "identifier", "created_at"),
        Index("ix_login_attempts_ip_created_at", "ip_address", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    identifier: Mapped[str] = mapped_column(String(320), nullable=False)
    ip_address: Mapped[str | None] = mapped_column(String(64))
    successful: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class EmailToken(Base):
    """A single-use, expiring token sent to an email address (verify / reset)."""

    __tablename__ = "email_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_email_tokens_token_hash"),
        Index("ix_email_tokens_user_id_purpose", "user_id", "purpose"),
        Index("ix_email_tokens_expires_at", "expires_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    purpose: Mapped[EmailTokenPurpose] = mapped_column(
        enum_column(EmailTokenPurpose, "email_token_purpose"), nullable=False
    )
    token_hash: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    requested_ip: Mapped[str | None] = mapped_column(String(64))

    user: Mapped[User] = relationship()
