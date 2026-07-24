"""Community features: kudos (shout-outs) and daily check-ins.

Both are strictly cohort-scoped, like everything else a member sees.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, TimestampMixin

if TYPE_CHECKING:
    from app.models.user import User


class Kudos(Base, TimestampMixin):
    """A public shout-out from one member to another, inside one cohort."""

    __tablename__ = "kudos"
    __table_args__ = (
        Index("ix_kudos_cohort_id_created_at", "cohort_id", "created_at"),
        Index("ix_kudos_cohort_recipient", "cohort_id", "to_user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False
    )
    from_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    to_user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    message: Mapped[str] = mapped_column(String(280), nullable=False)
    # Optional provenance: the help request or message this kudos is thanking for.
    help_request_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("help_requests.id", ondelete="SET NULL"), nullable=True
    )
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )

    from_user: Mapped[User] = relationship(foreign_keys=[from_user_id], lazy="joined")
    to_user: Mapped[User] = relationship(foreign_keys=[to_user_id], lazy="joined")


class CheckIn(Base, TimestampMixin):
    """A lightweight "what I'm working on today" post, inside one cohort."""

    __tablename__ = "check_ins"
    __table_args__ = (
        Index("ix_check_ins_cohort_id_created_at", "cohort_id", "created_at"),
        Index("ix_check_ins_cohort_user", "cohort_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(String(500), nullable=False)

    user: Mapped[User] = relationship(lazy="joined")
