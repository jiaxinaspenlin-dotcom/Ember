"""Channel message routes, including the polling cursor endpoint."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import CohortDep, DbDep
from app.schemas.common import OkResponse
from app.schemas.content import (
    MessageCreateRequest,
    MessageEditRequest,
    MessageOut,
    MessagePage,
    NewMessagesResponse,
    ReadReceiptRequest,
)
from app.schemas.serializers import message_out
from app.services import channels, messages

router = APIRouter(prefix="/api/messages", tags=["messages"])


@router.get(
    "/channel/{channel_id}",
    response_model=MessagePage,
    summary="Paginated channel history (newest page first)",
)
def list_channel_messages(
    channel_id: uuid.UUID,
    db: DbDep,
    ctx: CohortDep,
    before_seq: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> MessagePage:
    channel = channels.get_channel(db, ctx.cohort, channel_id)
    rows = messages.list_messages(db, channel=channel, before_seq=before_seq, limit=limit)
    views = messages.build_views(db, rows, viewer=ctx.member)
    oldest = rows[0].seq if rows else None
    return MessagePage(
        items=[message_out(view) for view in views],
        has_older=messages.has_older_messages(db, channel=channel, oldest_seq=oldest),
        oldest_seq=oldest,
        latest_seq=messages.latest_seq(db, channel=channel),
    )


@router.get(
    "/channel/{channel_id}/new",
    response_model=NewMessagesResponse,
    summary="Poll for messages newer than a cursor",
)
def poll_channel_messages(
    channel_id: uuid.UUID,
    db: DbDep,
    ctx: CohortDep,
    after_seq: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> NewMessagesResponse:
    """Returns *only* messages newer than ``after_seq`` -- never full history."""

    channel = channels.get_channel(db, ctx.cohort, channel_id)
    rows = messages.list_new_messages(db, channel=channel, after_seq=after_seq, limit=limit)
    views = messages.build_views(db, rows, viewer=ctx.member)
    return NewMessagesResponse(
        items=[message_out(view) for view in views],
        latest_seq=rows[-1].seq if rows else after_seq,
        count=len(rows),
    )


@router.post(
    "/channel/{channel_id}",
    response_model=MessageOut,
    status_code=status.HTTP_201_CREATED,
    summary="Post a message to a channel",
)
def create_channel_message(
    channel_id: uuid.UUID, payload: MessageCreateRequest, db: DbDep, ctx: CohortDep
) -> MessageOut:
    channel = channels.get_channel(db, ctx.cohort, channel_id)
    message = messages.create_message(
        db,
        actor=ctx.member,
        channel=channel,
        body=payload.body,
        parent_message_id=payload.parent_message_id,
    )
    db.commit()
    db.refresh(message)
    return message_out(messages.build_view(db, message, viewer=ctx.member))


@router.get("/{message_id}", response_model=MessageOut, summary="Read a single message")
def read_message(message_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> MessageOut:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    return message_out(messages.build_view(db, message, viewer=ctx.member))


@router.patch("/{message_id}", response_model=MessageOut, summary="Edit your own message")
def edit_message(
    message_id: uuid.UUID, payload: MessageEditRequest, db: DbDep, ctx: CohortDep
) -> MessageOut:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    messages.edit_message(db, actor=ctx.member, message=message, body=payload.body)
    db.commit()
    return message_out(messages.build_view(db, message, viewer=ctx.member))


@router.delete("/{message_id}", response_model=MessageOut, summary="Delete a message")
def delete_message(message_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> MessageOut:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    messages.soft_delete_message(db, actor=ctx.member, message=message)
    db.commit()
    return message_out(messages.build_view(db, message, viewer=ctx.member))


@router.post("/{message_id}/pin", response_model=MessageOut, summary="Pin a channel message")
def pin_message(message_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> MessageOut:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    messages.pin_message(db, actor=ctx.member, message=message)
    db.commit()
    return message_out(messages.build_view(db, message, viewer=ctx.member))


@router.delete("/{message_id}/pin", response_model=MessageOut, summary="Unpin a message")
def unpin_message(message_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> MessageOut:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    messages.unpin_message(db, actor=ctx.member, message=message)
    db.commit()
    return message_out(messages.build_view(db, message, viewer=ctx.member))


@router.get(
    "/channel/{channel_id}/pinned",
    response_model=list[MessageOut],
    summary="Pinned messages in a channel",
)
def list_pinned(channel_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> list[MessageOut]:
    channel = channels.get_channel(db, ctx.cohort, channel_id)
    rows = messages.list_pinned(db, channel=channel)
    return [message_out(view) for view in messages.build_views(db, rows, viewer=ctx.member)]


@router.put(
    "/channel/{channel_id}/read",
    response_model=OkResponse,
    summary="Update your read position in a channel",
)
def mark_channel_read(
    channel_id: uuid.UUID, payload: ReadReceiptRequest, db: DbDep, ctx: CohortDep
) -> OkResponse:
    channel = channels.get_channel(db, ctx.cohort, channel_id)
    messages.update_read_receipt(
        db,
        actor=ctx.member,
        channel=channel,
        last_read_message_id=payload.last_read_message_id,
    )
    db.commit()
    return OkResponse()


@router.get(
    "/channel/{channel_id}/unread",
    response_model=dict[str, int],
    summary="Unread count for a channel",
)
def channel_unread(channel_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> dict[str, int]:
    channel = channels.get_channel(db, ctx.cohort, channel_id)
    return {
        "unread_count": messages.unread_count_for_channel(
            db, user_id=ctx.user_id, channel_id=channel.id
        )
    }
