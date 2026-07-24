"""Reaction routes."""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from app.api.dependencies import CohortDep, DbDep
from app.schemas.content import MessageOut, ReactionRequest
from app.schemas.serializers import message_out
from app.services import messages

router = APIRouter(prefix="/api/reactions", tags=["reactions"])


@router.post("/{message_id}", response_model=MessageOut, summary="Add a reaction")
def add_reaction(
    message_id: uuid.UUID, payload: ReactionRequest, db: DbDep, ctx: CohortDep
) -> MessageOut:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    messages.add_reaction(
        db, actor=ctx.member, message=message, reaction_type=payload.reaction_type
    )
    db.commit()
    db.refresh(message)
    return message_out(messages.build_view(db, message, viewer=ctx.member))


@router.delete("/{message_id}", response_model=MessageOut, summary="Remove a reaction")
def remove_reaction(
    message_id: uuid.UUID, payload: ReactionRequest, db: DbDep, ctx: CohortDep
) -> MessageOut:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    messages.remove_reaction(
        db, actor=ctx.member, message=message, reaction_type=payload.reaction_type
    )
    db.commit()
    db.refresh(message)
    return message_out(messages.build_view(db, message, viewer=ctx.member))


@router.post("/{message_id}/toggle", response_model=MessageOut, summary="Toggle a reaction")
def toggle_reaction(
    message_id: uuid.UUID, payload: ReactionRequest, db: DbDep, ctx: CohortDep
) -> MessageOut:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    messages.toggle_reaction(
        db, actor=ctx.member, message=message, reaction_type=payload.reaction_type
    )
    db.commit()
    db.refresh(message)
    return message_out(messages.build_view(db, message, viewer=ctx.member))
