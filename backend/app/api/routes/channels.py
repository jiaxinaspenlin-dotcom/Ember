"""Channel routes (cohort-scoped)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, status

from app.api.dependencies import CohortDep, DbDep, PaginationDep
from app.core.config import settings
from app.schemas.common import OkResponse, Page, UserSummary
from app.schemas.content import (
    ChannelCreateRequest,
    ChannelInviteCodeOut,
    ChannelInviteRequest,
    ChannelJoinByCodeRequest,
    ChannelListItemOut,
    ChannelOut,
    ChannelUpdateRequest,
)
from app.schemas.serializers import channel_list_item, user_summary
from app.services import channels, cohorts

router = APIRouter(prefix="/api/channels", tags=["channels"])


def _invite_url(invite_code: str) -> str:
    return f"{settings.frontend_url.rstrip('/')}/channels/join/{invite_code}"


@router.get("", response_model=Page[ChannelListItemOut], summary="List channels")
def list_channels(
    db: DbDep,
    ctx: CohortDep,
    pagination: PaginationDep,
    include_archived: bool = False,
    only_archived: bool = False,
) -> Page[ChannelListItemOut]:
    items, total = channels.list_channels(
        db,
        cohort=ctx.cohort,
        user=ctx.user,
        include_archived=include_archived,
        only_archived=only_archived,
        limit=pagination.limit,
        offset=pagination.offset,
    )
    return Page[ChannelListItemOut](
        items=[channel_list_item(item) for item in items],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        has_more=pagination.offset + len(items) < total,
    )


@router.post(
    "", response_model=ChannelOut, status_code=status.HTTP_201_CREATED, summary="Create a channel"
)
def create_channel(payload: ChannelCreateRequest, db: DbDep, ctx: CohortDep) -> ChannelOut:
    channel = channels.create_channel(
        db,
        actor=ctx.member,
        name=payload.name,
        description=payload.description,
        topic=payload.topic,
    )
    db.commit()
    return ChannelOut.model_validate(channel)


@router.post(
    "/join-by-code", response_model=ChannelOut, summary="Join a channel with an invite code"
)
def join_by_code(payload: ChannelJoinByCodeRequest, db: DbDep, ctx: CohortDep) -> ChannelOut:
    channel = channels.join_by_invite(db, actor=ctx.member, invite_code=payload.invite_code)
    db.commit()
    return ChannelOut.model_validate(channel)


@router.get("/{channel_id}", response_model=ChannelOut, summary="Read one channel")
def read_channel(channel_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> ChannelOut:
    return ChannelOut.model_validate(channels.get_channel(db, ctx.cohort, channel_id))


@router.patch("/{channel_id}", response_model=ChannelOut, summary="Rename or retopic a channel")
def update_channel(
    channel_id: uuid.UUID, payload: ChannelUpdateRequest, db: DbDep, ctx: CohortDep
) -> ChannelOut:
    channel = channels.get_channel(db, ctx.cohort, channel_id)
    channels.rename_channel(
        db,
        actor=ctx.member,
        channel=channel,
        name=payload.name,
        description=payload.description,
        topic=payload.topic,
    )
    db.commit()
    return ChannelOut.model_validate(channel)


@router.post("/{channel_id}/archive", response_model=ChannelOut, summary="Archive a channel")
def archive_channel(channel_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> ChannelOut:
    channel = channels.archive_channel(
        db, actor=ctx.member, channel=channels.get_channel(db, ctx.cohort, channel_id)
    )
    db.commit()
    return ChannelOut.model_validate(channel)


@router.post("/{channel_id}/restore", response_model=ChannelOut, summary="Restore a channel")
def restore_channel(channel_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> ChannelOut:
    channel = channels.restore_channel(
        db, actor=ctx.member, channel=channels.get_channel(db, ctx.cohort, channel_id)
    )
    db.commit()
    return ChannelOut.model_validate(channel)


@router.post("/{channel_id}/join", response_model=OkResponse, summary="Join a channel")
def join_channel(channel_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> OkResponse:
    channel = channels.get_channel(db, ctx.cohort, channel_id)
    channels.join_channel(db, actor=ctx.member, channel=channel)
    db.commit()
    return OkResponse()


@router.post("/{channel_id}/leave", response_model=OkResponse, summary="Leave a channel")
def leave_channel(channel_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> OkResponse:
    channels.leave_channel(
        db, actor=ctx.member, channel=channels.get_channel(db, ctx.cohort, channel_id)
    )
    db.commit()
    return OkResponse()


@router.post(
    "/{channel_id}/members",
    response_model=OkResponse,
    summary="Invite a member to a channel (channel admin)",
)
def invite_member(
    channel_id: uuid.UUID, payload: ChannelInviteRequest, db: DbDep, ctx: CohortDep
) -> OkResponse:
    invitee = cohorts.get_membership(db, cohort_id=ctx.cohort_id, user_id=payload.user_id)
    if invitee is None:
        from app.core.errors import NotFoundError

        raise NotFoundError("Member not found.", code="USER_NOT_FOUND")
    channel = channels.get_channel(db, ctx.cohort, channel_id)
    channels.invite_member(db, actor=ctx.member, channel=channel, invitee=invitee)
    db.commit()
    return OkResponse()


@router.delete(
    "/{channel_id}/members/{user_id}",
    response_model=OkResponse,
    summary="Remove a member from a channel (channel admin)",
)
def remove_member(
    channel_id: uuid.UUID, user_id: uuid.UUID, db: DbDep, ctx: CohortDep
) -> OkResponse:
    from app.services import accounts

    channels.remove_member(
        db,
        actor=ctx.member,
        channel=channels.get_channel(db, ctx.cohort, channel_id),
        member=accounts.require_user(db, user_id),
    )
    db.commit()
    return OkResponse()


@router.post(
    "/{channel_id}/invite-code",
    response_model=ChannelInviteCodeOut,
    summary="Create or rotate the channel's invite link (channel admin)",
)
def create_invite_code(channel_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> ChannelInviteCodeOut:
    code = channels.generate_invite_code(
        db, actor=ctx.member, channel=channels.get_channel(db, ctx.cohort, channel_id)
    )
    db.commit()
    return ChannelInviteCodeOut(invite_code=code, invite_url=_invite_url(code))


@router.delete(
    "/{channel_id}/invite-code",
    response_model=OkResponse,
    summary="Turn off the channel's invite link (channel admin)",
)
def revoke_invite_code(channel_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> OkResponse:
    channels.revoke_invite_code(
        db, actor=ctx.member, channel=channels.get_channel(db, ctx.cohort, channel_id)
    )
    db.commit()
    return OkResponse()


@router.get(
    "/{channel_id}/members", response_model=Page[UserSummary], summary="List channel members"
)
def list_channel_members(
    channel_id: uuid.UUID, db: DbDep, ctx: CohortDep, pagination: PaginationDep
) -> Page[UserSummary]:
    channel = channels.get_channel(db, ctx.cohort, channel_id)
    members, total = channels.list_members(
        db, channel=channel, limit=pagination.limit, offset=pagination.offset
    )
    return Page[UserSummary](
        items=[user_summary(member) for member in members],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        has_more=pagination.offset + len(members) < total,
    )
