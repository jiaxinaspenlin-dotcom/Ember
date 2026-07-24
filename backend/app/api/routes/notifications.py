"""Notification routes. A user can only ever see their own notifications."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.dependencies import CohortDep, DbDep, PaginationDep
from app.schemas.common import Page
from app.schemas.content import NotificationOut
from app.schemas.serializers import notification_out
from app.services import notifications

router = APIRouter(prefix="/api/notifications", tags=["notifications"])


@router.get("", response_model=Page[NotificationOut], summary="Your notifications")
def list_notifications(
    db: DbDep, ctx: CohortDep, pagination: PaginationDep, unread_only: bool = False
) -> Page[NotificationOut]:
    items, total = notifications.list_for_user(
        db,
        cohort_id=ctx.cohort_id,
        user=ctx.user,
        unread_only=unread_only,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[NotificationOut](
        items=[notification_out(item) for item in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        has_more=pagination.offset + len(items) < total,
    )


@router.get("/unread-count", response_model=dict[str, int], summary="Unread notification count")
def unread_count(db: DbDep, ctx: CohortDep) -> dict[str, int]:
    return {
        "unread_count": notifications.unread_count(
            db, cohort_id=ctx.cohort_id, user_id=ctx.user_id
        )
    }


@router.post(
    "/{notification_id}/read", response_model=NotificationOut, summary="Mark one as read"
)
def mark_read(notification_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> NotificationOut:
    notification = notifications.mark_notification_read(
        db, user=ctx.user, notification_id=notification_id
    )
    db.commit()
    return notification_out(notification)


@router.post("/read-all", response_model=dict[str, int], summary="Mark all as read")
def mark_all_read(db: DbDep, ctx: CohortDep) -> dict[str, int]:
    updated = notifications.mark_all_read(db, cohort_id=ctx.cohort_id, user=ctx.user)
    db.commit()
    return {"updated": updated}
