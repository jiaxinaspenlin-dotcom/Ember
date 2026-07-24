"""Thread routes. Replies are loaded only when a thread is opened."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.dependencies import CohortDep, DbDep, PaginationDep
from app.schemas.content import MessageCreateRequest, MessageOut, ThreadOut
from app.schemas.serializers import message_out, user_summary
from app.services import channels, direct_messages, messages

router = APIRouter(prefix="/api/threads", tags=["threads"])


@router.get("/{parent_id}", response_model=ThreadOut, summary="Open a thread")
def read_thread(
    parent_id: uuid.UUID, db: DbDep, ctx: CohortDep, pagination: PaginationDep
) -> ThreadOut:
    parent = messages.get_visible_message(db, message_id=parent_id, actor=ctx.member)
    replies = messages.list_thread_replies(
        db, parent=parent, limit=pagination.limit, offset=pagination.offset
    )
    if parent.channel_id is not None:
        channel = channels.get_channel(db, ctx.cohort, parent.channel_id)
        source_label = f"#{channel.name}"
    else:
        source_label = "Direct message"
    return ThreadOut(
        parent=message_out(messages.build_view(db, parent, viewer=ctx.member)),
        replies=[
            message_out(view)
            for view in messages.build_views(db, replies, viewer=ctx.member)
        ],
        participants=[
            user_summary(user) for user in messages.thread_participants(db, parent=parent)
        ],
        source_label=source_label,
        reply_count=parent.reply_count,
    )


@router.post(
    "/{parent_id}/replies",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Reply in a thread",
)
def create_reply(
    parent_id: uuid.UUID, payload: MessageCreateRequest, db: DbDep, ctx: CohortDep
) -> MessageOut:
    parent = messages.get_visible_message(db, message_id=parent_id, actor=ctx.member)
    if parent.channel_id is not None:
        channel = channels.get_channel(db, ctx.cohort, parent.channel_id)
        reply = messages.create_message(
            db,
            actor=ctx.member,
            channel=channel,
            body=payload.body,
            parent_message_id=parent.id,
        )
    else:
        assert parent.direct_conversation_id is not None
        conversation = direct_messages.get_conversation(
            db, parent.direct_conversation_id, actor=ctx.member
        )
        reply = messages.create_message(
            db,
            actor=ctx.member,
            conversation=conversation,
            body=payload.body,
            parent_message_id=parent.id,
        )
    db.commit()
    db.refresh(reply)
    return message_out(messages.build_view(db, reply, viewer=ctx.member))
