"""Direct message pages and threads."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.dependencies import DbDep
from app.services import channels, direct_messages, messages
from app.web.deps import PageCohort, page_context
from app.web.templating import render

router = APIRouter(tags=["web-dm"])

PAGE_SIZE = 40


@router.get("/dm", response_class=HTMLResponse, summary="Direct messages")
def conversations_page(request: Request, db: DbDep, ctx: PageCohort) -> Response:
    items, total = direct_messages.list_conversations(
        db, cohort=ctx.cohort, user=ctx.user, limit=50
    )
    return render(
        request,
        "pages/conversations.html",
        page_context(db, ctx, conversations=items, total=total, active_nav="dm"),
    )


@router.post("/dm/start", summary="Start or open a conversation")
def start_conversation(
    db: DbDep, ctx: PageCohort, user_id: Annotated[uuid.UUID, Form()]
) -> Response:
    conversation = direct_messages.get_or_create_conversation(
        db, actor=ctx.member, other_user_id=user_id
    )
    db.commit()
    return RedirectResponse(
        f"/dm/{conversation.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/dm/{conversation_id}", response_class=HTMLResponse, summary="A conversation")
def conversation_page(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    conversation_id: uuid.UUID,
    before_seq: Annotated[int | None, Query(ge=0)] = None,
) -> Response:
    conversation = direct_messages.get_conversation(db, conversation_id, actor=ctx.member)
    other = direct_messages.other_member(conversation, user_id=ctx.user_id)
    rows = messages.list_messages(
        db, conversation=conversation, before_seq=before_seq, limit=PAGE_SIZE
    )
    views = messages.build_views(db, rows, viewer=ctx.member)
    if rows:
        messages.update_read_receipt(db, actor=ctx.member, conversation=conversation)
        db.commit()
    return render(
        request,
        "pages/conversation.html",
        page_context(
            db,
            ctx,
            conversation=conversation,
            other_member=other,
            message_views=views,
            latest_seq=messages.latest_seq(db, conversation=conversation),
            oldest_seq=rows[0].seq if rows else None,
            has_older=messages.has_older_messages(
                db, conversation=conversation, oldest_seq=rows[0].seq if rows else None
            ),
            active_nav="dm",
        ),
    )


@router.post(
    "/hx/dm/{conversation_id}/messages",
    response_class=HTMLResponse,
    summary="Send a direct message (HTMX)",
)
def send_direct_message(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    conversation_id: uuid.UUID,
    body: Annotated[str, Form()],
) -> Response:
    conversation = direct_messages.get_conversation(db, conversation_id, actor=ctx.member)
    message = messages.create_message(
        db, actor=ctx.member, conversation=conversation, body=body
    )
    db.commit()
    db.refresh(message)
    view = messages.build_view(db, message, viewer=ctx.member)
    return render(
        request,
        "fragments/message_appended.html",
        {
            "view": view,
            "current_user": ctx.user,
            "latest_seq": message.seq,
            "poll_url": f"/hx/dm/{conversation_id}/stream",
        },
    )


@router.get(
    "/hx/dm/{conversation_id}/stream",
    response_class=HTMLResponse,
    summary="Poll a conversation for newer messages (HTMX)",
)
def stream_direct_messages(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    conversation_id: uuid.UUID,
    after_seq: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    conversation = direct_messages.get_conversation(db, conversation_id, actor=ctx.member)
    rows = messages.list_new_messages(
        db, conversation=conversation, after_seq=after_seq, limit=50
    )
    views = messages.build_views(db, rows, viewer=ctx.member)
    if rows:
        messages.update_read_receipt(db, actor=ctx.member, conversation=conversation)
        db.commit()
    return render(
        request,
        "fragments/message_stream.html",
        {
            "views": views,
            "current_user": ctx.user,
            "latest_seq": rows[-1].seq if rows else after_seq,
            "poll_url": f"/hx/dm/{conversation_id}/stream",
        },
    )


@router.get(
    "/hx/dm/{conversation_id}/older",
    response_class=HTMLResponse,
    summary="Load older direct messages (HTMX)",
)
def load_older(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    conversation_id: uuid.UUID,
    before_seq: Annotated[int, Query(ge=0)],
) -> Response:
    conversation = direct_messages.get_conversation(db, conversation_id, actor=ctx.member)
    rows = messages.list_messages(
        db, conversation=conversation, before_seq=before_seq, limit=PAGE_SIZE
    )
    views = messages.build_views(db, rows, viewer=ctx.member)
    oldest = rows[0].seq if rows else None
    return render(
        request,
        "fragments/older_messages.html",
        {
            "views": views,
            "current_user": ctx.user,
            "oldest_seq": oldest,
            "has_older": messages.has_older_messages(
                db, conversation=conversation, oldest_seq=oldest
            ),
            "older_url": f"/hx/dm/{conversation_id}/older",
        },
    )


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


@router.get("/threads/{parent_id}", response_class=HTMLResponse, summary="A thread")
def thread_page(
    request: Request, db: DbDep, ctx: PageCohort, parent_id: uuid.UUID
) -> Response:
    parent = messages.get_visible_message(db, message_id=parent_id, actor=ctx.member)
    replies = messages.list_thread_replies(db, parent=parent)
    if parent.channel_id is not None:
        channel = channels.get_channel(db, ctx.cohort, parent.channel_id)
        source_label = f"#{channel.name}"
        back_link = f"/channels/{channel.slug}"
        can_reply = not channel.is_archived
    else:
        source_label = "Direct message"
        back_link = f"/dm/{parent.direct_conversation_id}"
        can_reply = True
    return render(
        request,
        "pages/thread.html",
        page_context(
            db,
            ctx,
            parent_view=messages.build_view(db, parent, viewer=ctx.member),
            reply_views=messages.build_views(db, replies, viewer=ctx.member),
            participants=messages.thread_participants(db, parent=parent),
            source_label=source_label,
            back_link=back_link,
            can_reply=can_reply,
            active_nav="channels" if parent.channel_id else "dm",
        ),
    )


@router.post(
    "/hx/threads/{parent_id}/replies",
    response_class=HTMLResponse,
    summary="Reply in a thread (HTMX)",
)
def create_reply(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    parent_id: uuid.UUID,
    body: Annotated[str, Form()],
) -> Response:
    parent = messages.get_visible_message(db, message_id=parent_id, actor=ctx.member)
    if parent.channel_id is not None:
        channel = channels.get_channel(db, ctx.cohort, parent.channel_id)
        reply = messages.create_message(
            db,
            actor=ctx.member,
            channel=channel,
            body=body,
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
            body=body,
            parent_message_id=parent.id,
        )
    db.commit()
    db.refresh(reply)
    view = messages.build_view(db, reply, viewer=ctx.member)
    return render(
        request,
        "fragments/thread_reply.html",
        {"view": view, "current_user": ctx.user},
    )
