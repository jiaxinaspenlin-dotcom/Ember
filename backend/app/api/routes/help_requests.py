"""Help request routes and the Help Queue."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import CohortDep, DbDep, PaginationDep
from app.core.enums import HelpCategory, HelpRequestStatus, Priority
from app.schemas.common import Page
from app.schemas.content import (
    HelpRequestCreateRequest,
    HelpRequestOut,
    HelpRequestUpdateRequest,
    HelpResolveRequest,
)
from app.schemas.serializers import help_request_out
from app.services import help_requests, messages

router = APIRouter(prefix="/api/help-requests", tags=["help-requests"])


@router.get("", response_model=Page[HelpRequestOut], summary="The Help Queue")
def list_help_requests(
    db: DbDep,
    ctx: CohortDep,
    pagination: PaginationDep,
    status_filter: Annotated[HelpRequestStatus | None, Query(alias="status")] = None,
    category: HelpCategory | None = None,
    urgency: Priority | None = None,
    assigned_to_me: bool = False,
    created_by_me: bool = False,
    unclaimed: bool = False,
    q: Annotated[str | None, Query(max_length=120)] = None,
) -> Page[HelpRequestOut]:
    items, total = help_requests.list_help_requests(
        db,
        cohort=ctx.cohort,
        user=ctx.user,
        filters=help_requests.HelpFilters(
            status=status_filter,
            category=category,
            urgency=urgency,
            assigned_to_me=assigned_to_me,
            created_by_me=created_by_me,
            unclaimed=unclaimed,
            query=q,
        ),
        limit=pagination.limit,
        offset=pagination.offset,
    )
    views = help_requests.build_views(items, viewer=ctx.member)
    return Page[HelpRequestOut](
        items=[help_request_out(view) for view in views],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        has_more=pagination.offset + len(items) < total,
    )


@router.post(
    "",
    response_model=HelpRequestOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a help request (optionally from a message)",
)
def create_help_request(
    payload: HelpRequestCreateRequest, db: DbDep, ctx: CohortDep
) -> HelpRequestOut:
    source = (
        messages.get_visible_message(
            db, message_id=payload.source_message_id, actor=ctx.member
        )
        if payload.source_message_id
        else None
    )
    help_request = help_requests.create_help_request(
        db,
        requester=ctx.member,
        title=payload.title,
        description=payload.description,
        category=payload.category,
        urgency=payload.urgency,
        source_message=source,
    )
    db.commit()
    return help_request_out(help_requests.build_view(help_request, viewer=ctx.member))


@router.get("/{help_request_id}", response_model=HelpRequestOut, summary="Read a help request")
def read_help_request(
    help_request_id: uuid.UUID, db: DbDep, ctx: CohortDep
) -> HelpRequestOut:
    help_request = help_requests.get_help_request(db, ctx.cohort, help_request_id)
    return help_request_out(help_requests.build_view(help_request, viewer=ctx.member))


@router.patch("/{help_request_id}", response_model=HelpRequestOut, summary="Edit a help request")
def update_help_request(
    help_request_id: uuid.UUID,
    payload: HelpRequestUpdateRequest,
    db: DbDep,
    ctx: CohortDep,
) -> HelpRequestOut:
    help_request = help_requests.update_help_request(
        db,
        actor=ctx.member,
        help_request=help_requests.get_help_request(db, ctx.cohort, help_request_id),
        title=payload.title,
        description=payload.description,
        category=payload.category,
        urgency=payload.urgency,
    )
    db.commit()
    return help_request_out(help_requests.build_view(help_request, viewer=ctx.member))


@router.post("/{help_request_id}/claim", response_model=HelpRequestOut, summary="Claim")
def claim(help_request_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> HelpRequestOut:
    help_request = help_requests.claim_help_request(
        db,
        actor=ctx.member,
        help_request=help_requests.get_help_request(db, ctx.cohort, help_request_id),
    )
    db.commit()
    return help_request_out(help_requests.build_view(help_request, viewer=ctx.member))


@router.post("/{help_request_id}/unclaim", response_model=HelpRequestOut, summary="Unclaim")
def unclaim(help_request_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> HelpRequestOut:
    help_request = help_requests.unclaim_help_request(
        db,
        actor=ctx.member,
        help_request=help_requests.get_help_request(db, ctx.cohort, help_request_id),
    )
    db.commit()
    return help_request_out(help_requests.build_view(help_request, viewer=ctx.member))


@router.post("/{help_request_id}/resolve", response_model=HelpRequestOut, summary="Resolve")
def resolve(
    help_request_id: uuid.UUID, payload: HelpResolveRequest, db: DbDep, ctx: CohortDep
) -> HelpRequestOut:
    help_request = help_requests.resolve_help_request(
        db,
        actor=ctx.member,
        help_request=help_requests.get_help_request(db, ctx.cohort, help_request_id),
        resolution_note=payload.resolution_note,
    )
    db.commit()
    return help_request_out(help_requests.build_view(help_request, viewer=ctx.member))


@router.post("/{help_request_id}/cancel", response_model=HelpRequestOut, summary="Cancel")
def cancel(help_request_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> HelpRequestOut:
    help_request = help_requests.cancel_help_request(
        db,
        actor=ctx.member,
        help_request=help_requests.get_help_request(db, ctx.cohort, help_request_id),
    )
    db.commit()
    return help_request_out(help_requests.build_view(help_request, viewer=ctx.member))


@router.post("/{help_request_id}/reopen", response_model=HelpRequestOut, summary="Reopen")
def reopen(help_request_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> HelpRequestOut:
    help_request = help_requests.reopen_help_request(
        db,
        actor=ctx.member,
        help_request=help_requests.get_help_request(db, ctx.cohort, help_request_id),
    )
    db.commit()
    return help_request_out(help_requests.build_view(help_request, viewer=ctx.member))
