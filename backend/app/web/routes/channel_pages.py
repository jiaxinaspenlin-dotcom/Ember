"""Channel pages plus the HTMX fragments that drive messaging."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.dependencies import DbDep
from app.auth import permissions
from app.core.config import settings
from app.core.enums import ReactionType
from app.core.errors import EmberError, NotFoundError, ValidationError
from app.models.user import User
from app.services import accounts, channels, cohorts, messages, profiles
from app.web.deps import PageCohort, page_context
from app.web.templating import render

router = APIRouter(tags=["web-channels"])

PAGE_SIZE = 40


@router.get("/channels", response_class=HTMLResponse, summary="Channel directory")
def channels_page(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    archived: bool = False,
) -> Response:
    items, total = channels.list_channels(
        db, cohort=ctx.cohort, user=ctx.user, only_archived=archived, limit=100
    )
    return render(
        request,
        "pages/channels.html",
        page_context(
            db,
            ctx,
            channel_items=items,
            total=total,
            showing_archived=archived,
            active_nav="channels",
        ),
    )


@router.post("/channels", response_class=HTMLResponse, summary="Create a channel")
def create_channel(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    topic: Annotated[str, Form()] = "",
) -> Response:
    permissions.require_channel_create(ctx.member)
    try:
        channel = channels.create_channel(
            db, actor=ctx.member, name=name, description=description, topic=topic
        )
        db.commit()
    except EmberError as exc:
        db.rollback()
        items, total = channels.list_channels(db, cohort=ctx.cohort, user=ctx.user, limit=100)
        return render(
            request,
            "pages/channels.html",
            page_context(
                db,
                ctx,
                channel_items=items,
                total=total,
                showing_archived=False,
                error_message=exc.message,
                active_nav="channels",
            ),
            status_code=exc.status_code,
        )
    return RedirectResponse(
        f"/channels/{channel.slug}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/channels/{slug}", response_class=HTMLResponse, summary="A channel")
def channel_page(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    slug: str,
    before_seq: Annotated[int | None, Query(ge=0)] = None,
) -> Response:
    channel = channels.get_channel_by_slug(db, ctx.cohort, slug)
    rows = messages.list_messages(
        db, channel=channel, before_seq=before_seq, limit=PAGE_SIZE
    )
    views = messages.build_views(db, rows, viewer=ctx.member)
    capabilities = permissions.channel_capabilities(db, channel, ctx.member)

    if capabilities["is_member"] and rows:
        messages.update_read_receipt(db, actor=ctx.member, channel=channel)
        db.commit()

    latest_seq = messages.latest_seq(db, channel=channel)

    # The manage panel (channel admin only) needs the member list, who can still
    # be invited, and the current invite link. Only computed when it will be shown.
    channel_members: list[User] = []
    invitable: list[User] = []
    invite_url: str | None = None
    if capabilities["can_manage"]:
        channel_members, _ = channels.list_members(db, channel=channel, limit=200)
        member_ids = {member.id for member in channel_members}
        directory, _ = profiles.list_directory(
            db, cohort=ctx.cohort, filters=profiles.DirectoryFilters(), limit=200
        )
        invitable = [m.user for m in directory if m.user_id not in member_ids]
        if channel.invite_code:
            invite_url = (
                f"{settings.frontend_url.rstrip('/')}/channels/join/{channel.invite_code}"
            )

    return render(
        request,
        "pages/channel.html",
        page_context(
            db,
            ctx,
            channel=channel,
            message_views=views,
            capabilities=capabilities,
            pinned=messages.list_pinned(db, channel=channel),
            member_count=channels.member_count(db, channel.id),
            channel_members=channel_members,
            invitable_members=invitable,
            invite_url=invite_url,
            latest_seq=latest_seq,
            oldest_seq=rows[0].seq if rows else None,
            has_older=messages.has_older_messages(
                db, channel=channel, oldest_seq=rows[0].seq if rows else None
            ),
            active_nav="channels",
        ),
    )


@router.post("/channels/{slug}/join", summary="Join a channel")
def join_channel(slug: str, db: DbDep, ctx: PageCohort) -> Response:
    channel = channels.get_channel_by_slug(db, ctx.cohort, slug)
    channels.join_channel(db, actor=ctx.member, channel=channel)
    db.commit()
    return RedirectResponse(f"/channels/{slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/channels/{slug}/leave", summary="Leave a channel")
def leave_channel(slug: str, db: DbDep, ctx: PageCohort) -> Response:
    channel = channels.get_channel_by_slug(db, ctx.cohort, slug)
    channels.leave_channel(db, actor=ctx.member, channel=channel)
    db.commit()
    return RedirectResponse("/channels", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/channels/{slug}/archive", summary="Archive a channel")
def archive_channel(slug: str, db: DbDep, ctx: PageCohort) -> Response:
    channels.archive_channel(
        db, actor=ctx.member, channel=channels.get_channel_by_slug(db, ctx.cohort, slug)
    )
    db.commit()
    return RedirectResponse(f"/channels/{slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/channels/{slug}/restore", summary="Restore a channel")
def restore_channel(slug: str, db: DbDep, ctx: PageCohort) -> Response:
    channels.restore_channel(
        db, actor=ctx.member, channel=channels.get_channel_by_slug(db, ctx.cohort, slug)
    )
    db.commit()
    return RedirectResponse(f"/channels/{slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/channels/{slug}/settings", summary="Update channel settings")
def update_channel(
    slug: str,
    db: DbDep,
    ctx: PageCohort,
    name: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    topic: Annotated[str, Form()] = "",
) -> Response:
    channel = channels.get_channel_by_slug(db, ctx.cohort, slug)
    channels.rename_channel(
        db,
        actor=ctx.member,
        channel=channel,
        name=name,
        description=description,
        topic=topic,
    )
    db.commit()
    return RedirectResponse(f"/channels/{channel.slug}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Membership: invites, removal, shareable link
# ---------------------------------------------------------------------------


@router.post("/channels/{slug}/invite", summary="Invite a member to a channel")
def invite_member(
    slug: str, db: DbDep, ctx: PageCohort, user_id: Annotated[uuid.UUID, Form()]
) -> Response:
    channel = channels.get_channel_by_slug(db, ctx.cohort, slug)
    invitee_m = cohorts.get_membership(db, cohort_id=ctx.cohort_id, user_id=user_id)
    if invitee_m is None:
        raise NotFoundError("Member not found.", code="USER_NOT_FOUND")
    channels.invite_member(db, actor=ctx.member, channel=channel, invitee=invitee_m)
    db.commit()
    return RedirectResponse(f"/channels/{slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/channels/{slug}/members/{user_id}/remove", summary="Remove a channel member")
def remove_member(
    slug: str, user_id: uuid.UUID, db: DbDep, ctx: PageCohort
) -> Response:
    channel = channels.get_channel_by_slug(db, ctx.cohort, slug)
    channels.remove_member(
        db, actor=ctx.member, channel=channel, member=accounts.require_user(db, user_id)
    )
    db.commit()
    return RedirectResponse(f"/channels/{slug}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/channels/{slug}/invite-link", summary="Create or rotate the invite link")
def create_invite_link(slug: str, db: DbDep, ctx: PageCohort) -> Response:
    channel = channels.get_channel_by_slug(db, ctx.cohort, slug)
    channels.generate_invite_code(db, actor=ctx.member, channel=channel)
    db.commit()
    return RedirectResponse(f"/channels/{slug}#invite", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/channels/{slug}/invite-link/revoke", summary="Turn off the invite link")
def revoke_invite_link(slug: str, db: DbDep, ctx: PageCohort) -> Response:
    channel = channels.get_channel_by_slug(db, ctx.cohort, slug)
    channels.revoke_invite_code(db, actor=ctx.member, channel=channel)
    db.commit()
    return RedirectResponse(f"/channels/{slug}#invite", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/channels/join/{invite_code}", summary="Join a channel from an invite link")
def join_by_invite(invite_code: str, db: DbDep, ctx: PageCohort) -> Response:
    channel = channels.join_by_invite(db, actor=ctx.member, invite_code=invite_code)
    db.commit()
    return RedirectResponse(f"/channels/{channel.slug}", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# HTMX fragments
# ---------------------------------------------------------------------------


@router.post(
    "/hx/channels/{slug}/messages",
    response_class=HTMLResponse,
    summary="Send a channel message (HTMX)",
)
def send_message(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    slug: str,
    body: Annotated[str, Form()],
) -> Response:
    channel = channels.get_channel_by_slug(db, ctx.cohort, slug)
    message = messages.create_message(db, actor=ctx.member, channel=channel, body=body)
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
            "poll_url": f"/hx/channels/{slug}/stream",
        },
    )


@router.get(
    "/hx/channels/{slug}/stream",
    response_class=HTMLResponse,
    summary="Poll for newer channel messages (HTMX)",
)
def stream_messages(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    slug: str,
    after_seq: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    """Returns only messages newer than ``after_seq``; empty when nothing is new."""

    channel = channels.get_channel_by_slug(db, ctx.cohort, slug)
    rows = messages.list_new_messages(db, channel=channel, after_seq=after_seq, limit=50)
    views = messages.build_views(db, rows, viewer=ctx.member)
    if rows and permissions.is_channel_member(db, channel.id, ctx.user_id):
        messages.update_read_receipt(db, actor=ctx.member, channel=channel)
        db.commit()
    return render(
        request,
        "fragments/message_stream.html",
        {
            "views": views,
            "current_user": ctx.user,
            "latest_seq": rows[-1].seq if rows else after_seq,
            "channel": channel,
            "poll_url": f"/hx/channels/{slug}/stream",
        },
    )


@router.get(
    "/hx/channels/{slug}/older",
    response_class=HTMLResponse,
    summary="Load older channel messages (HTMX)",
)
def load_older(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    slug: str,
    before_seq: Annotated[int, Query(ge=0)],
) -> Response:
    channel = channels.get_channel_by_slug(db, ctx.cohort, slug)
    rows = messages.list_messages(
        db, channel=channel, before_seq=before_seq, limit=PAGE_SIZE
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
                db, channel=channel, oldest_seq=oldest
            ),
            "older_url": f"/hx/channels/{slug}/older",
        },
    )


@router.post(
    "/hx/messages/{message_id}/react",
    response_class=HTMLResponse,
    summary="Toggle a reaction (HTMX)",
)
def toggle_reaction(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    message_id: uuid.UUID,
    reaction_type: Annotated[str, Form()],
) -> Response:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    try:
        parsed = ReactionType(reaction_type)
    except ValueError as exc:
        raise ValidationError("That reaction is not supported.") from exc
    messages.toggle_reaction(db, actor=ctx.member, message=message, reaction_type=parsed)
    db.commit()
    db.refresh(message)
    view = messages.build_view(db, message, viewer=ctx.member)
    return render(
        request,
        "fragments/reaction_bar.html",
        {"view": view, "current_user": ctx.user},
    )


@router.post(
    "/hx/messages/{message_id}/pin",
    response_class=HTMLResponse,
    summary="Pin or unpin (HTMX)",
)
def toggle_pin(
    request: Request, db: DbDep, ctx: PageCohort, message_id: uuid.UUID
) -> Response:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    if message.is_pinned:
        messages.unpin_message(db, actor=ctx.member, message=message)
    else:
        messages.pin_message(db, actor=ctx.member, message=message)
    db.commit()
    db.refresh(message)
    view = messages.build_view(db, message, viewer=ctx.member)
    return render(
        request,
        "fragments/message_row.html",
        {"view": view, "current_user": ctx.user, "swap_oob": False},
    )


@router.post(
    "/hx/messages/{message_id}/delete",
    response_class=HTMLResponse,
    summary="Delete a message (HTMX)",
)
def delete_message(
    request: Request, db: DbDep, ctx: PageCohort, message_id: uuid.UUID
) -> Response:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    messages.soft_delete_message(db, actor=ctx.member, message=message)
    db.commit()
    db.refresh(message)
    view = messages.build_view(db, message, viewer=ctx.member)
    return render(
        request,
        "fragments/message_row.html",
        {"view": view, "current_user": ctx.user, "swap_oob": False},
    )


@router.get(
    "/hx/messages/{message_id}/edit",
    response_class=HTMLResponse,
    summary="Inline edit form (HTMX)",
)
def edit_form(
    request: Request, db: DbDep, ctx: PageCohort, message_id: uuid.UUID
) -> Response:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    permissions.require_message_edit(message, ctx.member)
    return render(
        request,
        "fragments/message_edit_form.html",
        {"message": message, "current_user": ctx.user},
    )


@router.post(
    "/hx/messages/{message_id}/edit",
    response_class=HTMLResponse,
    summary="Save an inline edit (HTMX)",
)
def save_edit(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    message_id: uuid.UUID,
    body: Annotated[str, Form()],
) -> Response:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    messages.edit_message(db, actor=ctx.member, message=message, body=body)
    db.commit()
    db.refresh(message)
    view = messages.build_view(db, message, viewer=ctx.member)
    return render(
        request,
        "fragments/message_row.html",
        {"view": view, "current_user": ctx.user, "swap_oob": False},
    )


@router.get(
    "/hx/messages/{message_id}/cancel-edit",
    response_class=HTMLResponse,
    summary="Cancel an inline edit (HTMX)",
)
def cancel_edit(
    request: Request, db: DbDep, ctx: PageCohort, message_id: uuid.UUID
) -> Response:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    view = messages.build_view(db, message, viewer=ctx.member)
    return render(
        request,
        "fragments/message_row.html",
        {"view": view, "current_user": ctx.user, "swap_oob": False},
    )
