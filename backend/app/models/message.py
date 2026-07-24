"""Messages, reactions and mentions."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING, Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Sequence,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import MentionType, MessageType, ReactionType
from app.db.base import Base
from app.models.types import enum_column

if TYPE_CHECKING:
    from app.models.channel import Channel, DirectConversation
    from app.models.user import User

# One global sequence gives every message a total order, which makes the
# polling cursor ("give me everything after N") a single indexed comparison.
MESSAGE_SEQ = Sequence("message_seq", metadata=Base.metadata)


class Message(Base):
    """A message in exactly one destination: a channel *or* a direct conversation."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "(channel_id IS NOT NULL AND direct_conversation_id IS NULL)"
            " OR (channel_id IS NULL AND direct_conversation_id IS NOT NULL)",
            name="exactly_one_destination",
        ),
        Index("ix_messages_cohort_id_created_at", "cohort_id", "created_at"),
        Index("ix_messages_channel_id_created_at", "channel_id", "created_at"),
        Index("ix_messages_channel_id_id", "channel_id", "id"),
        Index("ix_messages_conversation_id_created_at", "direct_conversation_id", "created_at"),
        Index("ix_messages_parent_message_id", "parent_message_id"),
        Index("ix_messages_sender_id_created_at", "sender_id", "created_at"),
        Index("ix_messages_is_pinned", "is_pinned"),
        Index("ix_messages_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False
    )
    # Monotonic, installation-wide ordering key backed by a real PostgreSQL
    # sequence. The polling cursor and unread counts are both built on it, so it
    # must be assigned by the database, never by the application.
    seq: Mapped[int] = mapped_column(
        BigInteger,
        MESSAGE_SEQ,
        server_default=MESSAGE_SEQ.next_value(),
        unique=True,
        nullable=False,
        index=True,
    )
    sender_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    direct_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("direct_conversations.id", ondelete="CASCADE")
    )
    parent_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE")
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    message_type: Mapped[MessageType] = mapped_column(
        enum_column(MessageType, "message_type"), default=MessageType.TEXT, nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    edited_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    pinned_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    pinned_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reply_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_reply_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('english', coalesce(body, ''))", persisted=True),
    )

    sender: Mapped[User] = relationship(foreign_keys=[sender_id], lazy="joined")
    channel: Mapped[Channel | None] = relationship()
    conversation: Mapped[DirectConversation | None] = relationship()
    parent: Mapped[Message | None] = relationship(remote_side=[id], back_populates="replies")
    replies: Mapped[list[Message]] = relationship(back_populates="parent", viewonly=True)
    reactions: Mapped[list[Reaction]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )

    @property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @property
    def is_thread_reply(self) -> bool:
        return self.parent_message_id is not None


class Reaction(Base):
    """One reaction type from one user on one message."""

    __tablename__ = "reactions"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "user_id", "reaction_type", name="uq_reactions_message_user_type"
        ),
        Index("ix_reactions_message_id", "message_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    reaction_type: Mapped[ReactionType] = mapped_column(
        enum_column(ReactionType, "reaction_type"), nullable=False
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    message: Mapped[Message] = relationship(back_populates="reactions")
    user: Mapped[User] = relationship(lazy="joined")


class Mention(Base):
    """A mention parsed out of a message body by the Python parser."""

    __tablename__ = "mentions"
    __table_args__ = (
        Index("ix_mentions_message_id", "message_id"),
        Index("ix_mentions_mentioned_user_id", "mentioned_user_id"),
        CheckConstraint(
            "(mention_type = 'user' AND mentioned_user_id IS NOT NULL)"
            " OR (mention_type <> 'user')",
            name="user_mention_requires_user",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    message_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    mention_type: Mapped[MentionType] = mapped_column(
        enum_column(MentionType, "mention_type"), nullable=False
    )
    mentioned_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE")
    )
    raw_text: Mapped[str] = mapped_column(String(120), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    message: Mapped[Message] = relationship()
    mentioned_user: Mapped[User | None] = relationship()


class ReadReceipt(Base):
    """Per-user read position in a channel or conversation."""

    __tablename__ = "read_receipts"
    __table_args__ = (
        CheckConstraint(
            "(channel_id IS NOT NULL AND direct_conversation_id IS NULL)"
            " OR (channel_id IS NULL AND direct_conversation_id IS NOT NULL)",
            name="exactly_one_scope",
        ),
        UniqueConstraint("user_id", "channel_id", name="uq_read_receipts_user_channel"),
        UniqueConstraint(
            "user_id", "direct_conversation_id", name="uq_read_receipts_user_conversation"
        ),
        Index("ix_read_receipts_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("channels.id", ondelete="CASCADE")
    )
    direct_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("direct_conversations.id", ondelete="CASCADE")
    )
    last_read_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    last_read_seq: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    last_read_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
