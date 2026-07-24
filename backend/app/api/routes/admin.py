"""Cohort administrator routes.

There is no global installation admin any more: ``/api/admin`` is the console
for the admin *of a single cohort*. Every number and action here is fenced to
the caller's active cohort. Platform-wide operations live in the
``ember-admin`` CLI, not in any HTTP surface.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.dependencies import AdminCohortDep, DbDep, PaginationDep
from app.core.enums import UserRole
from app.core.errors import NotFoundError
from app.models.action import Decision, HelpRequest, Task
from app.models.channel import Channel
from app.models.cohort import CohortMembership
from app.models.message import Message
from app.schemas.common import Page, UserSummary
from app.schemas.serializers import user_summary
from app.services import audit, cohorts

router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminStats(BaseModel):
    members: int
    admins: int
    channels: int
    archived_channels: int
    messages: int
    help_requests: int
    decisions: int
    tasks: int
    audit_events: int


class RoleUpdateRequest(BaseModel):
    role: UserRole


class AuditEventOut(BaseModel):
    id: uuid.UUID
    action: str
    actor_id: uuid.UUID | None
    entity_type: str | None
    entity_id: uuid.UUID | None
    created_at: str


@router.get("/stats", response_model=AdminStats, summary="Cohort statistics")
def read_stats(db: DbDep, ctx: AdminCohortDep) -> AdminStats:
    cohort_id = ctx.cohort_id

    def count(model: Any, *conditions: Any) -> int:
        stmt = select(func.count()).select_from(model).where(model.cohort_id == cohort_id)
        for condition in conditions:
            stmt = stmt.where(condition)
        return int(db.scalar(stmt) or 0)

    return AdminStats(
        members=cohorts.member_count(db, cohort_id),
        admins=int(
            db.scalar(
                select(func.count())
                .select_from(CohortMembership)
                .where(
                    CohortMembership.cohort_id == cohort_id,
                    CohortMembership.role == UserRole.ADMIN,
                )
            )
            or 0
        ),
        channels=count(Channel, Channel.is_archived.is_(False)),
        archived_channels=count(Channel, Channel.is_archived.is_(True)),
        messages=count(Message),
        help_requests=count(HelpRequest),
        decisions=count(Decision),
        tasks=count(Task),
        audit_events=audit.count_events(db, cohort_id=cohort_id),
    )


@router.get("/users", response_model=Page[UserSummary], summary="List cohort members")
def list_users(db: DbDep, ctx: AdminCohortDep, pagination: PaginationDep) -> Page[UserSummary]:
    members, total = cohorts.list_members(
        db, cohort=ctx.cohort, limit=pagination.limit, offset=pagination.offset
    )
    return Page[UserSummary](
        items=[user_summary(m.user, role=m.role.value) for m in members],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        has_more=pagination.offset + len(members) < total,
    )


@router.put(
    "/users/{user_id}/role", response_model=UserSummary, summary="Change a member's role"
)
def set_role(
    user_id: uuid.UUID, payload: RoleUpdateRequest, db: DbDep, ctx: AdminCohortDep
) -> UserSummary:
    """Roles change only through this authenticated, audited admin route."""

    target = cohorts.get_membership(db, cohort_id=ctx.cohort_id, user_id=user_id)
    if target is None:
        raise NotFoundError("Member not found.", code="USER_NOT_FOUND")
    updated = cohorts.set_member_role(
        db, actor=ctx.member, target=target, role=payload.role
    )
    db.commit()
    return user_summary(updated.user, role=updated.role.value)


@router.get("/audit", response_model=list[AuditEventOut], summary="Recent audit events")
def list_audit(
    db: DbDep, ctx: AdminCohortDep, pagination: PaginationDep
) -> list[AuditEventOut]:
    events = audit.recent_events(
        db, cohort_id=ctx.cohort_id, limit=pagination.limit, offset=pagination.offset
    )
    return [
        AuditEventOut(
            id=event.id,
            action=event.action.value,
            actor_id=event.actor_id,
            entity_type=event.entity_type,
            entity_id=event.entity_id,
            created_at=event.created_at.isoformat(),
        )
        for event in events
    ]
