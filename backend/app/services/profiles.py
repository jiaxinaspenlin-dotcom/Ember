"""Per-cohort member profiles, skills and the member directory.

A profile is a ``CohortMembership``: the same person has a different bio,
project, skills and status in each cohort. Display name and avatar live on the
global ``User`` (you are the same person everywhere).
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.core.enums import AuditAction, WorkingStatus
from app.core.errors import ValidationError
from app.models.cohort import Cohort, CohortMembership, MembershipSkill, Skill
from app.models.user import User
from app.services import audit

MAX_SKILLS = 12
SKILL_MAX_LENGTH = 40


@dataclass(slots=True)
class DirectoryFilters:
    query: str | None = None
    skill: str | None = None
    working_status: WorkingStatus | None = None
    available_only: bool = False
    project_area: str | None = None


def slugify_skill(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return slug[:60]


def _resolve_skills(db: DbSession, names: list[str]) -> list[Skill]:
    """Find-or-create skill rows for the supplied names (global registry)."""

    resolved: list[Skill] = []
    seen: set[str] = set()
    for raw in names:
        name = " ".join(raw.split())[:SKILL_MAX_LENGTH]
        if not name:
            continue
        slug = slugify_skill(name)
        if not slug or slug in seen:
            continue
        seen.add(slug)
        skill = db.scalar(select(Skill).where(Skill.slug == slug))
        if skill is None:
            skill = Skill(slug=slug, name=name)
            db.add(skill)
            db.flush()
        resolved.append(skill)
        if len(resolved) >= MAX_SKILLS:
            break
    return resolved


def update_profile(
    db: DbSession,
    *,
    membership: CohortMembership,
    display_name: str | None = None,
    avatar_url: str | None = None,
    bio: str | None = None,
    skills: list[str] | None = None,
    current_project: str | None = None,
    project_area: str | None = None,
    working_status: WorkingStatus | None = None,
    available_to_help: bool | None = None,
) -> CohortMembership:
    """Targeted update. Display name and avatar are global; the rest per-cohort."""

    user = membership.user

    if display_name is not None:
        cleaned = " ".join(display_name.split())
        if len(cleaned) < 2:
            raise ValidationError(
                "Display name must be at least 2 characters.",
                details={"field": "display_name"},
            )
        user.display_name = cleaned[:120]
    if avatar_url is not None:
        stripped = avatar_url.strip()
        if stripped and not stripped.startswith(("http://", "https://")):
            raise ValidationError(
                "Avatar URL must start with http:// or https://",
                details={"field": "avatar_url"},
            )
        user.avatar_url = stripped[:500] or None
    if bio is not None:
        membership.bio = bio.strip()[:500] or None
    if current_project is not None:
        membership.current_project = current_project.strip()[:160] or None
    if project_area is not None:
        membership.project_area = project_area.strip()[:80] or None
    if working_status is not None:
        membership.working_status = working_status
        if working_status is WorkingStatus.AVAILABLE:
            membership.available_to_help = True
    if available_to_help is not None:
        membership.available_to_help = available_to_help

    if skills is not None:
        _replace_skills(db, membership, skills)

    if not membership.profile_completed and (
        membership.bio or membership.current_project or membership.skills
    ):
        membership.profile_completed = True

    audit.record(
        db,
        AuditAction.PROFILE_UPDATED,
        actor_id=user.id,
        cohort_id=membership.cohort_id,
        entity_type="cohort_membership",
        entity_id=membership.id,
    )
    db.flush()
    return membership


def _replace_skills(db: DbSession, membership: CohortMembership, names: list[str]) -> None:
    """Update the membership's skill links in place -- add/remove only what changed."""

    desired = _resolve_skills(db, names)
    desired_ids = {skill.id: index for index, skill in enumerate(desired)}
    existing = {link.skill_id: link for link in membership.skills}

    for skill_id, link in existing.items():
        if skill_id not in desired_ids:
            db.delete(link)
        else:
            link.position = desired_ids[skill_id]

    for skill in desired:
        if skill.id not in existing:
            db.add(
                MembershipSkill(
                    membership_id=membership.id,
                    skill_id=skill.id,
                    position=desired_ids[skill.id],
                )
            )
    db.flush()
    db.refresh(membership)


def set_working_status(
    db: DbSession, *, membership: CohortMembership, working_status: WorkingStatus
) -> CohortMembership:
    return update_profile(db, membership=membership, working_status=working_status)


# ---------------------------------------------------------------------------
# Directory (scoped to one cohort)
# ---------------------------------------------------------------------------


def _directory_query(
    cohort: Cohort, filters: DirectoryFilters
) -> Select[tuple[CohortMembership]]:
    stmt = (
        select(CohortMembership)
        .join(User, User.id == CohortMembership.user_id)
        .where(CohortMembership.cohort_id == cohort.id, User.is_active.is_(True))
    )
    if filters.query:
        pattern = f"%{filters.query.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(User.display_name).like(pattern),
                func.lower(func.coalesce(CohortMembership.current_project, "")).like(pattern),
                func.lower(func.coalesce(CohortMembership.bio, "")).like(pattern),
                func.lower(func.coalesce(CohortMembership.project_area, "")).like(pattern),
            )
        )
    if filters.working_status is not None:
        stmt = stmt.where(CohortMembership.working_status == filters.working_status)
    if filters.available_only:
        stmt = stmt.where(CohortMembership.available_to_help.is_(True))
    if filters.project_area:
        stmt = stmt.where(
            func.lower(func.coalesce(CohortMembership.project_area, ""))
            == filters.project_area.strip().lower()
        )
    if filters.skill:
        slug = slugify_skill(filters.skill)
        stmt = stmt.where(
            CohortMembership.id.in_(
                select(MembershipSkill.membership_id)
                .join(Skill, Skill.id == MembershipSkill.skill_id)
                .where(Skill.slug == slug)
            )
        )
    return stmt


def list_directory(
    db: DbSession,
    *,
    cohort: Cohort,
    filters: DirectoryFilters,
    exclude_user_id: uuid.UUID | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[CohortMembership], int]:
    stmt = _directory_query(cohort, filters)
    if exclude_user_id is not None:
        stmt = stmt.where(CohortMembership.user_id != exclude_user_id)

    total = int(
        db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery())) or 0
    )
    # ``_directory_query`` already joins ``User``; ordering reuses that join.
    rows = list(
        db.scalars(
            stmt.options(selectinload(CohortMembership.skills))
            .order_by(CohortMembership.available_to_help.desc(), User.display_name.asc())
            .limit(limit)
            .offset(offset)
        ).all()
    )
    return rows, total


def list_available_helpers(
    db: DbSession, *, cohort: Cohort, exclude_user_id: uuid.UUID, limit: int = 6
) -> list[CohortMembership]:
    return list(
        db.scalars(
            select(CohortMembership)
            .join(User, User.id == CohortMembership.user_id)
            .options(selectinload(CohortMembership.skills))
            .where(
                CohortMembership.cohort_id == cohort.id,
                User.is_active.is_(True),
                CohortMembership.user_id != exclude_user_id,
                CohortMembership.available_to_help.is_(True),
            )
            .order_by(User.display_name.asc())
            .limit(limit)
        ).all()
    )


def list_all_skills(db: DbSession, *, cohort: Cohort, limit: int = 100) -> list[Skill]:
    """Skills currently in use by at least one member of this cohort."""

    used = (
        select(MembershipSkill.skill_id)
        .join(CohortMembership, CohortMembership.id == MembershipSkill.membership_id)
        .where(CohortMembership.cohort_id == cohort.id)
        .distinct()
    )
    return list(
        db.scalars(
            select(Skill).where(Skill.id.in_(used)).order_by(Skill.name.asc()).limit(limit)
        ).all()
    )


def list_project_areas(db: DbSession, *, cohort: Cohort, limit: int = 100) -> list[str]:
    return [
        value
        for value in db.scalars(
            select(CohortMembership.project_area)
            .where(
                CohortMembership.cohort_id == cohort.id,
                CohortMembership.project_area.is_not(None),
            )
            .distinct()
            .order_by(CohortMembership.project_area.asc())
            .limit(limit)
        ).all()
        if value
    ]
