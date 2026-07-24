"""Decision Log routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import CohortDep, DbDep, PaginationDep
from app.core.enums import DecisionStatus
from app.schemas.common import Page
from app.schemas.content import (
    DecisionCreateRequest,
    DecisionOut,
    DecisionReverseRequest,
    DecisionSupersedeRequest,
    DecisionUpdateRequest,
)
from app.schemas.serializers import decision_out
from app.services import decisions, messages

router = APIRouter(prefix="/api/decisions", tags=["decisions"])


@router.get("", response_model=Page[DecisionOut], summary="Search the Decision Log")
def list_decisions(
    db: DbDep,
    ctx: CohortDep,
    pagination: PaginationDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status_filter: Annotated[DecisionStatus | None, Query(alias="status")] = None,
    channel_id: uuid.UUID | None = None,
    author_id: uuid.UUID | None = None,
    related_project: Annotated[str | None, Query(max_length=160)] = None,
) -> Page[DecisionOut]:
    items, total = decisions.list_decisions(
        db,
        cohort=ctx.cohort,
        filters=decisions.DecisionFilters(
            query=q,
            status=status_filter,
            channel_id=channel_id,
            author_id=author_id,
            related_project=related_project,
        ),
        limit=pagination.limit,
        offset=pagination.offset,
    )
    views = decisions.build_views(items, viewer=ctx.member)
    return Page[DecisionOut](
        items=[decision_out(view) for view in views],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        has_more=pagination.offset + len(items) < total,
    )


@router.post(
    "",
    response_model=DecisionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Record a decision (optionally from a message)",
)
def create_decision(payload: DecisionCreateRequest, db: DbDep, ctx: CohortDep) -> DecisionOut:
    source = (
        messages.get_visible_message(
            db, message_id=payload.source_message_id, actor=ctx.member
        )
        if payload.source_message_id
        else None
    )
    decision = decisions.create_decision(
        db,
        author=ctx.member,
        title=payload.title,
        decision_text=payload.decision_text,
        context=payload.context,
        related_project=payload.related_project,
        source_message=source,
    )
    db.commit()
    return decision_out(decisions.build_view(decision, viewer=ctx.member))


@router.get("/{decision_id}", response_model=DecisionOut, summary="Read a decision")
def read_decision(decision_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> DecisionOut:
    decision = decisions.get_decision(db, ctx.cohort, decision_id)
    return decision_out(decisions.build_view(decision, viewer=ctx.member))


@router.patch("/{decision_id}", response_model=DecisionOut, summary="Edit an active decision")
def update_decision(
    decision_id: uuid.UUID, payload: DecisionUpdateRequest, db: DbDep, ctx: CohortDep
) -> DecisionOut:
    decision = decisions.update_decision(
        db,
        actor=ctx.member,
        decision=decisions.get_decision(db, ctx.cohort, decision_id),
        title=payload.title,
        decision_text=payload.decision_text,
        context=payload.context,
        related_project=payload.related_project,
    )
    db.commit()
    return decision_out(decisions.build_view(decision, viewer=ctx.member))


@router.post(
    "/{decision_id}/supersede", response_model=DecisionOut, summary="Supersede a decision"
)
def supersede(
    decision_id: uuid.UUID, payload: DecisionSupersedeRequest, db: DbDep, ctx: CohortDep
) -> DecisionOut:
    decision = decisions.supersede_decision(
        db,
        actor=ctx.member,
        decision=decisions.get_decision(db, ctx.cohort, decision_id),
        replacement_id=payload.superseded_by_id,
    )
    db.commit()
    return decision_out(decisions.build_view(decision, viewer=ctx.member))


@router.post("/{decision_id}/reverse", response_model=DecisionOut, summary="Reverse a decision")
def reverse(
    decision_id: uuid.UUID, payload: DecisionReverseRequest, db: DbDep, ctx: CohortDep
) -> DecisionOut:
    decision = decisions.reverse_decision(
        db,
        actor=ctx.member,
        decision=decisions.get_decision(db, ctx.cohort, decision_id),
        reason=payload.reason,
    )
    db.commit()
    return decision_out(decisions.build_view(decision, viewer=ctx.member))
