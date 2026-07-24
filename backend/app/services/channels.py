"""Channel lifecycle and membership, scoped to a cohort.

Every channel belongs to exactly one cohort. The ``actor`` is a
:class:`CohortMembership`, so cohort scope and role come from one place.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.auth import permissions
from app.core.enums import AuditAction, NotificationType
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError, ValidationError
from app.db.base import utcnow
from app.models.channel import Channel, ChannelMember
from app.models.cohort import Cohort, CohortMembership
from app.models.message import Message, ReadReceipt
from app.models.user import User
from app.services import audit

SLUG_PATTERN = re.compile(r"[^a-z0-9-]+")

Actor = CohortMembership


@dataclass(slots=True)
class ChannelListItem:
    channel: Channel
    is_member: bool
    unread_count: int
    last_message_at: object | None


def slugify(name: str) -> str:
    slug = SLUG_PATTERN.sub("-", name.strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)[:60]


def get_channel(db: DbSession, cohort: Cohort, channel_id: uuid.UUID) -> Channel:
    channel = db.get(Channel, channel_id)
    if channel is None or channel.cohort_id != cohort.id:
        raise NotFoundError("Channel not found.", code="CHANNEL_NOT_FOUND")
    return channel


def get_channel_by_slug(db: DbSession, cohort: Cohort, slug: str) -> Channel:
    channel = db.scalar(
        select(Channel).where(Channel.cohort_id == cohort.id, Channel.slug == slug)
    )
    if channel is None:
        raise NotFoundError("Channel not found.", code="CHANNEL_NOT_FOUND")
    return channel


def create_channel(
    db: DbSession,
    *,
    actor: Actor,
    name: str,
    description: str | None = None,
    topic: str | None = None,
) -> Channel:
    permissions.require_channel_create(actor)

    clean_name = " ".join(name.split())
    if len(clean_name) < 2:
        raise ValidationError(
            "Channel name must be at least 2 characters.", details={"field": "name"}
        )
    if len(clean_name) > 80:
        raise ValidationError(
            "Channel name must be at most 80 characters.", details={"field": "name"}
        )
    slug = slugify(clean_name)
    if not slug:
        raise ValidationError(
            "Channel name must contain letters or numbers.", details={"field": "name"}
        )
    if (
        db.scalar(
            select(Channel).where(
                Channel.cohort_id == actor.cohort_id, Channel.slug == slug
            )
        )
        is not None
    ):
        raise ConflictError(
            "A channel with a similar name already exists.", code="CHANNEL_SLUG_TAKEN"
        )

    channel = Channel(
        cohort_id=actor.cohort_id,
        slug=slug,
        name=clean_name,
        description=(description or "").strip()[:300] or None,
        topic=(topic or "").strip()[:200] or None,
        created_by_id=actor.user_id,
    )
    db.add(channel)
    db.flush()

    db.add(ChannelMember(channel_id=channel.id, user_id=actor.user_id))
    audit.record(
        db,
        AuditAction.CHANNEL_CREATED,
        actor_id=actor.user_id,
        cohort_id=actor.cohort_id,
        entity_type="channel",
        entity_id=channel.id,
        context={"slug": slug},
    )
    try:
        db.flush()
    except IntegrityError as exc:  # pragma: no cover - concurrent create
        db.rollback()
        raise ConflictError(
            "A channel with a similar name already exists.", code="CHANNEL_SLUG_TAKEN"
        ) from exc
    return channel


def rename_channel(
    db: DbSession,
    *,
    actor: Actor,
    channel: Channel,
    name: str | None = None,
    description: str | None = None,
    topic: str | None = None,
) -> Channel:
    permissions.require_channel_management(channel, actor)
    changed: dict[str, str] = {}
    if name is not None:
        clean_name = " ".join(name.split())
        if len(clean_name) < 2:
            raise ValidationError(
                "Channel name must be at least 2 characters.", details={"field": "name"}
            )
        channel.name = clean_name[:80]
        changed["name"] = channel.name
    if description is not None:
        channel.description = description.strip()[:300] or None
        changed["description"] = channel.description or ""
    if topic is not None:
        channel.topic = topic.strip()[:200] or None
        changed["topic"] = channel.topic or ""
    audit.record(
        db,
        AuditAction.CHANNEL_RENAMED,
        actor_id=actor.user_id,
        cohort_id=channel.cohort_id,
        entity_type="channel",
        entity_id=channel.id,
        context=changed,
    )
    db.flush()
    return channel


def archive_channel(db: DbSession, *, actor: Actor, channel: Channel) -> Channel:
    permissions.require_channel_management(channel, actor)
    if channel.is_archived:
        raise ConflictError("This channel is already archived.", code="CHANNEL_ALREADY_ARCHIVED")
    channel.is_archived = True
    channel.archived_at = utcnow()
    channel.archived_by_id = actor.user_id
    audit.record(
        db,
        AuditAction.CHANNEL_ARCHIVED,
        actor_id=actor.user_id,
        cohort_id=channel.cohort_id,
        entity_type="channel",
        entity_id=channel.id,
    )
    db.flush()
    return channel


def restore_channel(db: DbSession, *, actor: Actor, channel: Channel) -> Channel:
    permissions.require_channel_management(channel, actor)
    if not channel.is_archived:
        raise ConflictError("This channel is not archived.", code="CHANNEL_NOT_ARCHIVED")
    channel.is_archived = False
    channel.archived_at = None
    channel.archived_by_id = None
    audit.record(
        db,
        AuditAction.CHANNEL_RESTORED,
        actor_id=actor.user_id,
        cohort_id=channel.cohort_id,
        entity_type="channel",
        entity_id=channel.id,
    )
    db.flush()
    return channel


def _join(db: DbSession, *, channel: Channel, user_id: uuid.UUID) -> ChannelMember:
    existing = db.scalar(
        select(ChannelMember).where(
            ChannelMember.channel_id == channel.id, ChannelMember.user_id == user_id
        )
    )
    if existing is not None:
        return existing
    membership = ChannelMember(channel_id=channel.id, user_id=user_id)
    db.add(membership)
    try:
        db.flush()
    except IntegrityError:  # pragma: no cover - concurrent join
        db.rollback()
        existing = db.scalar(
            select(ChannelMember).where(
                ChannelMember.channel_id == channel.id, ChannelMember.user_id == user_id
            )
        )
        if existing is None:
            raise
        return existing
    return membership


def join_channel(db: DbSession, *, actor: Actor, channel: Channel) -> ChannelMember:
    if channel.is_archived:
        raise PermissionDeniedError(
            "Archived channels cannot be joined.", code="CHANNEL_ARCHIVED"
        )
    membership = _join(db, channel=channel, user_id=actor.user_id)
    audit.record(
        db,
        AuditAction.CHANNEL_JOINED,
        actor_id=actor.user_id,
        cohort_id=channel.cohort_id,
        entity_type="channel",
        entity_id=channel.id,
    )
    db.flush()
    return membership


def leave_channel(db: DbSession, *, actor: Actor, channel: Channel) -> None:
    membership = db.scalar(
        select(ChannelMember).where(
            ChannelMember.channel_id == channel.id, ChannelMember.user_id == actor.user_id
        )
    )
    if membership is None:
        raise NotFoundError("You are not a member of this channel.", code="NOT_A_CHANNEL_MEMBER")
    db.delete(membership)
    audit.record(
        db,
        AuditAction.CHANNEL_LEFT,
        actor_id=actor.user_id,
        cohort_id=channel.cohort_id,
        entity_type="channel",
        entity_id=channel.id,
    )
    db.flush()


# ---------------------------------------------------------------------------
# Invitations and member management (channel admin = creator or cohort admin)
# ---------------------------------------------------------------------------


def invite_member(
    db: DbSession, *, actor: Actor, channel: Channel, invitee: CohortMembership
) -> ChannelMember:
    """Add a cohort member to a channel and notify them. Channel admin only."""

    from app.services import notifications

    permissions.require_channel_management(channel, actor)
    if channel.is_archived:
        raise ConflictError("This channel is archived.", code="CHANNEL_ARCHIVED")
    if invitee.cohort_id != channel.cohort_id:
        raise ValidationError(
            "That person is not in this cohort.", code="NOT_A_COHORT_MEMBER"
        )

    existing = db.scalar(
        select(ChannelMember).where(
            ChannelMember.channel_id == channel.id,
            ChannelMember.user_id == invitee.user_id,
        )
    )
    if existing is not None:
        return existing

    membership = _join(db, channel=channel, user_id=invitee.user_id)
    notifications.create_notification(
        db,
        cohort_id=channel.cohort_id,
        recipient_id=invitee.user_id,
        notification_type=NotificationType.CHANNEL_INVITE,
        title=f"{actor.user.display_name} added you to #{channel.name}",
        body=channel.topic or channel.description,
        link_path=f"/channels/{channel.slug}",
        actor_id=actor.user_id,
        channel_id=channel.id,
    )
    audit.record(
        db,
        AuditAction.CHANNEL_MEMBER_INVITED,
        actor_id=actor.user_id,
        cohort_id=channel.cohort_id,
        entity_type="channel",
        entity_id=channel.id,
        context={"invitee": str(invitee.user_id)},
    )
    db.flush()
    return membership


def remove_member(db: DbSession, *, actor: Actor, channel: Channel, member: User) -> None:
    permissions.require_channel_management(channel, actor)
    if member.id == channel.created_by_id:
        raise PermissionDeniedError(
            "The channel's creator cannot be removed.", code="CANNOT_REMOVE_CREATOR"
        )
    membership = db.scalar(
        select(ChannelMember).where(
            ChannelMember.channel_id == channel.id, ChannelMember.user_id == member.id
        )
    )
    if membership is None:
        raise NotFoundError(
            "That person is not a member of this channel.", code="NOT_A_CHANNEL_MEMBER"
        )
    db.delete(membership)
    audit.record(
        db,
        AuditAction.CHANNEL_MEMBER_REMOVED,
        actor_id=actor.user_id,
        cohort_id=channel.cohort_id,
        entity_type="channel",
        entity_id=channel.id,
        context={"removed": str(member.id)},
    )
    db.flush()


def generate_invite_code(db: DbSession, *, actor: Actor, channel: Channel) -> str:
    from app.core.security import generate_token

    permissions.require_channel_management(channel, actor)
    channel.invite_code = generate_token(18)
    audit.record(
        db,
        AuditAction.CHANNEL_INVITE_GENERATED,
        actor_id=actor.user_id,
        cohort_id=channel.cohort_id,
        entity_type="channel",
        entity_id=channel.id,
    )
    db.flush()
    return channel.invite_code


def revoke_invite_code(db: DbSession, *, actor: Actor, channel: Channel) -> None:
    permissions.require_channel_management(channel, actor)
    if channel.invite_code is None:
        return
    channel.invite_code = None
    audit.record(
        db,
        AuditAction.CHANNEL_INVITE_REVOKED,
        actor_id=actor.user_id,
        cohort_id=channel.cohort_id,
        entity_type="channel",
        entity_id=channel.id,
    )
    db.flush()


def get_channel_by_invite(db: DbSession, cohort: Cohort, invite_code: str) -> Channel | None:
    if not invite_code:
        return None
    return db.scalar(
        select(Channel).where(
            Channel.cohort_id == cohort.id, Channel.invite_code == invite_code
        )
    )


def join_by_invite(db: DbSession, *, actor: Actor, invite_code: str) -> Channel:
    channel = get_channel_by_invite(db, actor.cohort, invite_code)
    if channel is None:
        raise NotFoundError(
            "This invite link is invalid or has been turned off.", code="INVITE_INVALID"
        )
    if channel.is_archived:
        raise ConflictError(
            "This channel is archived and cannot be joined.", code="CHANNEL_ARCHIVED"
        )
    join_channel(db, actor=actor, channel=channel)
    return channel


# ---------------------------------------------------------------------------
# Listing (cohort-scoped)
# ---------------------------------------------------------------------------


def list_channels(
    db: DbSession,
    *,
    cohort: Cohort,
    user: User,
    include_archived: bool = False,
    only_archived: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[ChannelListItem], int]:
    membership = (
        select(ChannelMember.channel_id)
        .where(ChannelMember.user_id == user.id)
        .subquery()
    )
    read_seq = (
        select(ReadReceipt.channel_id, ReadReceipt.last_read_seq)
        .where(ReadReceipt.user_id == user.id, ReadReceipt.channel_id.is_not(None))
        .subquery()
    )
    last_message = (
        select(
            Message.channel_id.label("channel_id"),
            func.max(Message.created_at).label("last_message_at"),
        )
        .where(Message.channel_id.is_not(None), Message.deleted_at.is_(None))
        .group_by(Message.channel_id)
        .subquery()
    )
    unread = (
        select(
            Message.channel_id.label("channel_id"),
            func.count().label("unread_count"),
        )
        .join(read_seq, read_seq.c.channel_id == Message.channel_id, isouter=True)
        .where(
            Message.channel_id.is_not(None),
            Message.deleted_at.is_(None),
            Message.sender_id != user.id,
            Message.seq > func.coalesce(read_seq.c.last_read_seq, 0),
        )
        .group_by(Message.channel_id)
        .subquery()
    )

    stmt = (
        select(
            Channel,
            membership.c.channel_id.is_not(None).label("is_member"),
            func.coalesce(unread.c.unread_count, 0).label("unread_count"),
            last_message.c.last_message_at,
        )
        .join(membership, membership.c.channel_id == Channel.id, isouter=True)
        .join(unread, unread.c.channel_id == Channel.id, isouter=True)
        .join(last_message, last_message.c.channel_id == Channel.id, isouter=True)
        .where(Channel.cohort_id == cohort.id)
    )
    if only_archived:
        stmt = stmt.where(Channel.is_archived.is_(True))
    elif not include_archived:
        stmt = stmt.where(Channel.is_archived.is_(False))

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.execute(stmt.order_by(Channel.name.asc()).limit(limit).offset(offset)).all()
    items = [
        ChannelListItem(
            channel=row[0],
            is_member=bool(row[1]),
            unread_count=int(row[2] or 0),
            last_message_at=row[3],
        )
        for row in rows
    ]
    return items, total


def list_members(
    db: DbSession, *, channel: Channel, limit: int = 100, offset: int = 0
) -> tuple[list[User], int]:
    stmt = (
        select(User)
        .join(ChannelMember, ChannelMember.user_id == User.id)
        .where(ChannelMember.channel_id == channel.id)
    )
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    users = list(
        db.scalars(stmt.order_by(User.display_name.asc()).limit(limit).offset(offset)).all()
    )
    return users, total


def member_count(db: DbSession, channel_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(ChannelMember)
            .where(ChannelMember.channel_id == channel_id)
        )
        or 0
    )


def channel_count(db: DbSession, *, cohort: Cohort, include_archived: bool = False) -> int:
    stmt = select(func.count()).select_from(Channel).where(Channel.cohort_id == cohort.id)
    if not include_archived:
        stmt = stmt.where(Channel.is_archived.is_(False))
    return int(db.scalar(stmt) or 0)
