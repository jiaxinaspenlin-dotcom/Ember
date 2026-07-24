"""Announcement routes. Only cohort administrators may write."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.dependencies import AdminCohortDep, CohortDep, DbDep, PaginationDep
from app.schemas.common import OkResponse, Page
from app.schemas.content import (
    AnnouncementCreateRequest,
    AnnouncementOut,
    AnnouncementUpdateRequest,
)
from app.schemas.serializers import announcement_out
from app.services import announcements

router = APIRouter(prefix="/api/announcements", tags=["announcements"])


@router.get("", response_model=Page[AnnouncementOut], summary="List announcements")
def list_announcements(
    db: DbDep, ctx: CohortDep, pagination: PaginationDep, include_expired: bool = False
) -> Page[AnnouncementOut]:
    items, total = announcements.list_announcements(
        db,
        cohort=ctx.cohort,
        include_expired=include_expired,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[AnnouncementOut](
        items=[announcement_out(item) for item in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        has_more=pagination.offset + len(items) < total,
    )


@router.post(
    "",
    response_model=AnnouncementOut,
    status_code=status.HTTP_201_CREATED,
    summary="Publish an announcement (cohort administrators only)",
)
def create_announcement(
    payload: AnnouncementCreateRequest, db: DbDep, ctx: AdminCohortDep
) -> AnnouncementOut:
    announcement = announcements.create_announcement(
        db,
        author=ctx.member,
        title=payload.title,
        body=payload.body,
        priority=payload.priority,
        expires_at=payload.expires_at,
        is_pinned=payload.is_pinned,
    )
    db.commit()
    return announcement_out(announcement)


@router.patch(
    "/{announcement_id}", response_model=AnnouncementOut, summary="Update an announcement"
)
def update_announcement(
    announcement_id: uuid.UUID,
    payload: AnnouncementUpdateRequest,
    db: DbDep,
    ctx: AdminCohortDep,
) -> AnnouncementOut:
    announcement = announcements.update_announcement(
        db,
        actor=ctx.member,
        announcement=announcements.get_announcement(db, ctx.cohort, announcement_id),
        title=payload.title,
        body=payload.body,
        priority=payload.priority,
        expires_at=payload.expires_at,
        clear_expiry=payload.clear_expiry,
        is_pinned=payload.is_pinned,
    )
    db.commit()
    return announcement_out(announcement)


@router.delete(
    "/{announcement_id}", response_model=OkResponse, summary="Delete an announcement"
)
def delete_announcement(
    announcement_id: uuid.UUID, db: DbDep, ctx: AdminCohortDep
) -> OkResponse:
    announcements.delete_announcement(
        db,
        actor=ctx.member,
        announcement=announcements.get_announcement(db, ctx.cohort, announcement_id),
    )
    db.commit()
    return OkResponse()
