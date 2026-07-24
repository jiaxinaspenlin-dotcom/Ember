"""Cohorts (tenants) and per-cohort membership.

A **Cohort** is the top-level isolation boundary. A person's identity (the
``User`` row: email, credentials, OAuth, display name, avatar) is global, but
everything they *do* happens inside a cohort they belong to.

``CohortMembership`` is the join between a user and a cohort. It carries the
per-cohort role (member / admin) and the per-cohort profile — bio, current
project, skills, working status and availability all differ between cohorts.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.enums import UserRole, WorkingStatus
from app.db.base import Base, TimestampMixin
from app.models.types import enum_column

if TYPE_CHECKING:
    from app.models.user import User


class Cohort(Base, TimestampMixin):
    """A tenant: an isolated workspace for one cohort."""

    __tablename__ = "cohorts"
    __table_args__ = (
        UniqueConstraint("slug", name="uq_cohorts_slug"),
        UniqueConstraint("invite_code", name="uq_cohorts_invite_code"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str | None] = mapped_column(String(300))
    # Opaque shareable join code; null when no invite link is active.
    invite_code: Mapped[str | None] = mapped_column(String(64))
    # Null for system- or CLI-seeded cohorts that no member created.
    created_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )

    memberships: Mapped[list[CohortMembership]] = relationship(
        back_populates="cohort", cascade="all, delete-orphan"
    )


class CohortMembership(Base, TimestampMixin):
    """A user's participation in a cohort, with a per-cohort role and profile."""

    __tablename__ = "cohort_memberships"
    __table_args__ = (
        UniqueConstraint("cohort_id", "user_id", name="uq_cohort_memberships_cohort_user"),
        Index("ix_cohort_memberships_user_id", "user_id"),
        Index("ix_cohort_memberships_cohort_id_role", "cohort_id", "role"),
        Index("ix_cohort_memberships_available_to_help", "cohort_id", "available_to_help"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    cohort_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cohorts.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole, "cohort_role"), default=UserRole.MEMBER, nullable=False
    )
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- per-cohort profile ---------------------------------------------
    bio: Mapped[str | None] = mapped_column(String(500))
    current_project: Mapped[str | None] = mapped_column(String(160))
    project_area: Mapped[str | None] = mapped_column(String(80))
    working_status: Mapped[WorkingStatus] = mapped_column(
        enum_column(WorkingStatus, "working_status"),
        default=WorkingStatus.BUILDING,
        nullable=False,
    )
    available_to_help: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    profile_completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    cohort: Mapped[Cohort] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(lazy="joined")
    skills: Mapped[list[MembershipSkill]] = relationship(
        back_populates="membership",
        cascade="all, delete-orphan",
        order_by="MembershipSkill.position",
    )

    @property
    def is_admin(self) -> bool:
        return self.role is UserRole.ADMIN

    @property
    def skill_names(self) -> list[str]:
        return [link.skill.name for link in self.skills]


class Skill(Base):
    """A skill tag. Global registry; created on demand by members, never seeded."""

    __tablename__ = "skills"
    __table_args__ = (UniqueConstraint("slug", name="uq_skills_slug"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(60), nullable=False)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class MembershipSkill(Base):
    """Association between a cohort membership and a skill."""

    __tablename__ = "membership_skills"
    __table_args__ = (
        CheckConstraint("position >= 0", name="position_non_negative"),
        Index("ix_membership_skills_skill_id", "skill_id"),
    )

    membership_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("cohort_memberships.id", ondelete="CASCADE"), primary_key=True
    )
    skill_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("skills.id", ondelete="CASCADE"), primary_key=True
    )
    position: Mapped[int] = mapped_column(default=0, nullable=False)

    membership: Mapped[CohortMembership] = relationship(back_populates="skills")
    skill: Mapped[Skill] = relationship(lazy="joined")
