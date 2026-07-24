"""Channels, channel membership and direct conversations."""

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
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class Channel(Base, TimestampMixin):
    """A public cohort channel.

    Any member may create one; the ``created_by`` user is the channel's admin
    (they can invite, remove members, rename, archive and pin), independent of
    the installation-wide ``admin`` role.
    """

    __tablename__ = "channels"
    __table_args__ = (
        # Slugs and invite codes are unique *within* a cohort, not globally.
        UniqueConstraint("cohort_id", "slug", name="uq_channels_cohort_slug"),
        UniqueConstraint("invite_code", name="uq_channels_invite_code"),
        Index("ix_channels_cohort_id_is_archived", "cohort_id", "is_archived"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False
    )
    slug: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))
    topic: Mapped[str | None] = mapped_column(String(200))
    # Opaque shareable join code; null when no invite link is active.
    invite_code: Mapped[str | None] = mapped_column(String(64))
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    archived_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    archived_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )

    created_by: Mapped[User] = relationship(foreign_keys=[created_by_id])
    members: Mapped[list[ChannelMember]] = relationship(
        back_populates="channel", cascade="all, delete-orphan"
    )


class ChannelMember(Base):
    """Membership of a user in a channel."""

    __tablename__ = "channel_members"
    __table_args__ = (
        UniqueConstraint("channel_id", "user_id", name="uq_channel_members_channel_user"),
        Index("ix_channel_members_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    channel: Mapped[Channel] = relationship(back_populates="members")
    user: Mapped[User] = relationship()


class DirectConversation(Base):
    """A one-to-one private conversation between two members.

    ``pair_key`` is the canonical ``min(uuid):max(uuid)`` string for the two
    participants, giving us a database-level guarantee that a pair can only ever
    have one conversation.
    """

    __tablename__ = "direct_conversations"
    __table_args__ = (
        # A pair can have one conversation *per cohort* they share.
        UniqueConstraint("cohort_id", "pair_key", name="uq_direct_conversations_cohort_pair"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False
    )
    pair_key: Mapped[str] = mapped_column(String(80), nullable=False)
    created_by_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    last_message_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )

    members: Mapped[list[DirectConversationMember]] = relationship(
        back_populates="conversation", cascade="all, delete-orphan", lazy="selectin"
    )

    @staticmethod
    def build_pair_key(user_a: uuid.UUID, user_b: uuid.UUID) -> str:
        first, second = sorted([str(user_a), str(user_b)])
        return f"{first}:{second}"


class DirectConversationMember(Base):
    """Participation in a direct conversation. Enforced on every DM request."""

    __tablename__ = "direct_conversation_members"
    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "user_id", name="uq_direct_conversation_members_conversation_user"
        ),
        Index("ix_direct_conversation_members_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direct_conversations.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    conversation: Mapped[DirectConversation] = relationship(back_populates="members")
    user: Mapped[User] = relationship(lazy="joined")
