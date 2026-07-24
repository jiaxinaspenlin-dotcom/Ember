"""Direct message routes. Membership is verified on every request."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import CohortDep, DbDep, PaginationDep
from app.schemas.common import OkResponse, Page
from app.schemas.content import (
    ConversationCreateRequest,
    ConversationOut,
    MessageCreateRequest,
    MessageOut,
    MessagePage,
    NewMessagesResponse,
    ReadReceiptRequest,
)
from app.schemas.serializers import conversation_out, message_out
from app.services import direct_messages, messages

router = APIRouter(prefix="/api/direct-messages", tags=["direct-messages"])


@router.get("", response_model=Page[ConversationOut], summary="Your conversations")
def list_conversations(
    db: DbDep, ctx: CohortDep, pagination: PaginationDep
) -> Page[ConversationOut]:
    items, total = direct_messages.list_conversations(
        db, cohort=ctx.cohort, user=ctx.user, limit=pagination.limit, offset=pagination.offset
    )
    return Page[ConversationOut](
        items=[conversation_out(item) for item in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        has_more=pagination.offset + len(items) < total,
    )


@router.post(
    "",
    response_model=ConversationOut,
    status_code=status.HTTP_201_CREATED,
    summary="Open a conversation with a member",
)
def create_conversation(
    payload: ConversationCreateRequest, db: DbDep, ctx: CohortDep
) -> ConversationOut:
    conversation = direct_messages.get_or_create_conversation(
        db, actor=ctx.member, other_user_id=payload.user_id
    )
    db.commit()
    other = direct_messages.other_member(conversation, user_id=ctx.user_id)
    return conversation_out(
        direct_messages.ConversationListItem(
            conversation=conversation, other_member=other, unread_count=0, last_message=None
        )
    )


@router.get(
    "/{conversation_id}/messages",
    response_model=MessagePage,
    summary="Paginated conversation history",
)
def list_conversation_messages(
    conversation_id: uuid.UUID,
    db: DbDep,
    ctx: CohortDep,
    before_seq: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessagePage:
    conversation = direct_messages.get_conversation(db, conversation_id, actor=ctx.member)
    rows = messages.list_messages(
        db, conversation=conversation, before_seq=before_seq, limit=limit
    )
    views = messages.build_views(db, rows, viewer=ctx.member)
    oldest = rows[0].seq if rows else None
    return MessagePage(
        items=[message_out(view) for view in views],
        has_older=messages.has_older_messages(
            db, conversation=conversation, oldest_seq=oldest
        ),
        oldest_seq=oldest,
        latest_seq=messages.latest_seq(db, conversation=conversation),
    )


@router.get(
    "/{conversation_id}/messages/new",
    response_model=NewMessagesResponse,
    summary="Poll a conversation for newer messages",
)
def poll_conversation_messages(
    conversation_id: uuid.UUID,
    db: DbDep,
    ctx: CohortDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> NewMessagesResponse:
    conversation = direct_messages.get_conversation(db, conversation_id, actor=ctx.member)
    rows = messages.list_new_messages(
        db, conversation=conversation, after_seq=after_seq, limit=limit
    )
    views = messages.build_views(db, rows, viewer=ctx.member)
    return NewMessagesResponse(
        items=[message_out(view) for view in views],
        latest_seq=rows[-1].seq if rows else after_seq,
        count=len(rows),
    )


@router.post(
    "/{conversation_id}/messages",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Send a direct message",
)
def send_direct_message(
    conversation_id: uuid.UUID,
    payload: MessageCreateRequest,
    db: DbDep,
    ctx: CohortDep,
) -> MessageOut:
    conversation = direct_messages.get_conversation(db, conversation_id, actor=ctx.member)
    message = messages.create_message(
        db,
        actor=ctx.member,
        conversation=conversation,
        body=payload.body,
        parent_message_id=payload.parent_message_id,
    )
    db.commit()
    db.refresh(message)
    return message_out(messages.build_view(db, message, viewer=ctx.member))


@router.put(
    "/{conversation_id}/read",
    response_model=OkResponse,
    summary="Update your read position in a conversation",
)
def mark_conversation_read(
    conversation_id: uuid.UUID,
    payload: ReadReceiptRequest,
    db: DbDep,
    ctx: CohortDep,
) -> OkResponse:
    conversation = direct_messages.get_conversation(db, conversation_id, actor=ctx.member)
    messages.update_read_receipt(
        db,
        actor=ctx.member,
        conversation=conversation,
        last_read_message_id=payload.last_read_message_id,
    )
    db.commit()
    return OkResponse()
