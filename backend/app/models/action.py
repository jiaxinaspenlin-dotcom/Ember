"""Help requests, decisions and tasks -- the "turn conversation into action" models."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import (
    DecisionStatus,
    HelpCategory,
    HelpRequestStatus,
    Priority,
    TaskStatus,
)
from app.db.base import Base, TimestampMixin
from app.models.types import enum_column

if TYPE_CHECKING:
    from app.models.channel import Channel
    from app.models.message import Message
    from app.models.user import User


class HelpRequest(Base, TimestampMixin):
    """A request for help, optionally created from an existing channel message."""

    __tablename__ = "help_requests"
    __table_args__ = (
        Index("ix_help_requests_cohort_id_status", "cohort_id", "status"),
        Index("ix_help_requests_assigned_helper_id", "assigned_helper_id"),
        Index("ix_help_requests_requester_id", "requester_id"),
        Index("ix_help_requests_status_created_at", "status", "created_at"),
        CheckConstraint(
            "(status <> 'claimed') OR (assigned_helper_id IS NOT NULL AND claimed_at IS NOT NULL)",
            name="claimed_requires_helper",
        ),
        CheckConstraint(
            "(status <> 'resolved') OR (resolved_at IS NOT NULL)",
            name="resolved_requires_timestamp",
        ),
        CheckConstraint(
            "(status <> 'open') OR (assigned_helper_id IS NULL)",
            name="open_has_no_helper",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    original_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    requester_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    source_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL")
    )
    category: Mapped[HelpCategory] = mapped_column(
        enum_column(HelpCategory, "help_category"), default=HelpCategory.OTHER, nullable=False
    )
    urgency: Mapped[Priority] = mapped_column(
        enum_column(Priority, "help_urgency"), default=Priority.NORMAL, nullable=False
    )
    status: Mapped[HelpRequestStatus] = mapped_column(
        enum_column(HelpRequestStatus, "help_request_status"),
        default=HelpRequestStatus.OPEN,
        nullable=False,
    )
    assigned_helper_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    # Optional link to a related Forth item (validated https + exact host).
    forth_url: Mapped[str | None] = mapped_column(String(500))

    requester: Mapped[User] = relationship(foreign_keys=[requester_id], lazy="joined")
    assigned_helper: Mapped[User | None] = relationship(
        foreign_keys=[assigned_helper_id], lazy="joined"
    )
    source_channel: Mapped[Channel | None] = relationship(lazy="joined")
    original_message: Mapped[Message | None] = relationship()


class Decision(Base, TimestampMixin):
    """A recorded cohort decision."""

    __tablename__ = "decisions"
    __table_args__ = (
        Index("ix_decisions_cohort_id_status", "cohort_id", "status"),
        Index("ix_decisions_author_id", "author_id"),
        Index("ix_decisions_source_channel_id", "source_channel_id"),
        Index("ix_decisions_search_vector", "search_vector", postgresql_using="gin"),
        CheckConstraint(
            "(status = 'superseded' AND superseded_by_id IS NOT NULL)"
            " OR (status <> 'superseded' AND superseded_by_id IS NULL)",
            name="superseded_requires_replacement",
        ),
        CheckConstraint(
            "(status <> 'reversed') OR (reversed_at IS NOT NULL)",
            name="reversed_requires_timestamp",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    decision_text: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str | None] = mapped_column(Text)
    original_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    source_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL")
    )
    author_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    related_project: Mapped[str | None] = mapped_column(String(160))
    # Optional link to a related Forth item (validated https + exact host).
    forth_url: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[DecisionStatus] = mapped_column(
        enum_column(DecisionStatus, "decision_status"),
        default=DecisionStatus.ACTIVE,
        nullable=False,
    )
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("decisions.id", ondelete="SET NULL")
    )
    superseded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reversed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reversal_reason: Mapped[str | None] = mapped_column(Text)
    search_vector: Mapped[str | None] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('english', coalesce(title, '') || ' ' || coalesce(decision_text, '')"
            " || ' ' || coalesce(context, ''))",
            persisted=True,
        ),
    )

    author: Mapped[User] = relationship(foreign_keys=[author_id], lazy="joined")
    source_channel: Mapped[Channel | None] = relationship(lazy="joined")
    original_message: Mapped[Message | None] = relationship()
    superseded_by: Mapped[Decision | None] = relationship(remote_side=[id])


class Task(Base, TimestampMixin):
    """A unit of work, optionally created from a message."""

    __tablename__ = "tasks"
    __table_args__ = (
        Index("ix_tasks_assignee_id", "assignee_id"),
        Index("ix_tasks_status", "status"),
        Index("ix_tasks_assignee_id_status", "assignee_id", "status"),
        Index("ix_tasks_cohort_id_status", "cohort_id", "status"),
        Index("ix_tasks_creator_id", "creator_id"),
        CheckConstraint(
            "(status <> 'done') OR (completed_at IS NOT NULL)",
            name="done_requires_completed_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(160), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    # Optional link to a related Forth item (validated https + exact host).
    forth_url: Mapped[str | None] = mapped_column(String(500))
    creator_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
    )
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    source_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("channels.id", ondelete="SET NULL")
    )
    status: Mapped[TaskStatus] = mapped_column(
        enum_column(TaskStatus, "task_status"), default=TaskStatus.TODO, nullable=False
    )
    priority: Mapped[Priority] = mapped_column(
        enum_column(Priority, "task_priority"), default=Priority.NORMAL, nullable=False
    )
    due_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    creator: Mapped[User] = relationship(foreign_keys=[creator_id], lazy="joined")
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id], lazy="joined")
    source_channel: Mapped[Channel | None] = relationship(lazy="joined")
    source_message: Mapped[Message | None] = relationship()
