"""Member directory routes (scoped to the active cohort)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.api.dependencies import CohortDep, DbDep, PaginationDep
from app.core.enums import WorkingStatus
from app.core.errors import NotFoundError
from app.schemas.common import Page
from app.schemas.content import ProfileOut
from app.schemas.serializers import profile_out
from app.services import cohorts, profiles

router = APIRouter(prefix="/api/members", tags=["members"])


@router.get("", response_model=Page[ProfileOut], summary="Search the member directory")
def list_members(
    db: DbDep,
    ctx: CohortDep,
    pagination: PaginationDep,
    q: Annotated[str | None, Query(max_length=120)] = None,
    skill: Annotated[str | None, Query(max_length=60)] = None,
    working_status: WorkingStatus | None = None,
    available_only: bool = False,
    project_area: Annotated[str | None, Query(max_length=80)] = None,
    include_self: bool = False,
) -> Page[ProfileOut]:
    rows, total = profiles.list_directory(
        db,
        cohort=ctx.cohort,
        filters=profiles.DirectoryFilters(
            query=q,
            skill=skill,
            working_status=working_status,
            available_only=available_only,
            project_area=project_area,
        ),
        exclude_user_id=None if include_self else ctx.user_id,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[ProfileOut](
        items=[profile_out(membership) for membership in rows],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        has_more=pagination.offset + len(rows) < total,
    )


@router.get("/skills", response_model=list[str], summary="Skills in use across the cohort")
def list_skills(db: DbDep, ctx: CohortDep) -> list[str]:
    return [skill.name for skill in profiles.list_all_skills(db, cohort=ctx.cohort)]


@router.get("/project-areas", response_model=list[str], summary="Project areas in use")
def list_project_areas(db: DbDep, ctx: CohortDep) -> list[str]:
    return profiles.list_project_areas(db, cohort=ctx.cohort)


@router.get("/{user_id}", response_model=ProfileOut, summary="A single member profile")
def read_member(user_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> ProfileOut:
    membership = cohorts.get_membership(db, cohort_id=ctx.cohort_id, user_id=user_id)
    if membership is None:
        raise NotFoundError("Member not found.", code="USER_NOT_FOUND")
    return profile_out(membership)
