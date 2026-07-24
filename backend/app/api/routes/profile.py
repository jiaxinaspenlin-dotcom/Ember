"""Profile routes (your membership in the active cohort)."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import CohortDep, DbDep
from app.schemas.content import ProfileOut, ProfileUpdateRequest, WorkingStatusRequest
from app.schemas.serializers import profile_out
from app.services import profiles

router = APIRouter(prefix="/api/profile", tags=["profile"])


@router.get("", response_model=ProfileOut, summary="Your profile in this cohort")
def read_profile(db: DbDep, ctx: CohortDep) -> ProfileOut:
    del db
    return profile_out(ctx.member)


@router.patch("", response_model=ProfileOut, summary="Update your profile")
def update_profile(payload: ProfileUpdateRequest, db: DbDep, ctx: CohortDep) -> ProfileOut:
    membership = profiles.update_profile(
        db,
        membership=ctx.member,
        display_name=payload.display_name,
        avatar_url=payload.avatar_url,
        bio=payload.bio,
        skills=payload.skills,
        current_project=payload.current_project,
        project_area=payload.project_area,
        working_status=payload.working_status,
        available_to_help=payload.available_to_help,
    )
    db.commit()
    return profile_out(membership)


@router.put("/working-status", response_model=ProfileOut, summary="Set your working status")
def set_working_status(
    payload: WorkingStatusRequest, db: DbDep, ctx: CohortDep
) -> ProfileOut:
    membership = profiles.set_working_status(
        db, membership=ctx.member, working_status=payload.working_status
    )
    db.commit()
    return profile_out(membership)
