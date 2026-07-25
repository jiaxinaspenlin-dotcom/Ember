"""Messages, threads, reactions, pins, read receipts and the polling cursor.

Every function here enforces permissions itself, so a route can never bypass a
rule by forgetting a check.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.auth import permissions
from app.core.enums import AuditAction, MessageType, NotificationType, ReactionType
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.db.base import utcnow
from app.models.channel import Channel, ChannelMember, DirectConversation
from app.models.cohort import CohortMembership
from app.models.message import Message, Reaction, ReadReceipt
from app.models.user import User
from app.services import audit, forth, mentions, notifications

Actor = CohortMembership

MAX_BODY_LENGTH = 8000
EDIT_WINDOW = dt.timedelta(hours=24)


@dataclass(slots=True)
class ReactionSummary:
    reaction_type: ReactionType
    count: int
    reacted: bool
    participants: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MessageView:
    """A message plus everything the UI needs, pre-computed on the server."""

    message: Message
    reactions: list[ReactionSummary]
    can_edit: bool
    can_delete: bool
    can_pin: bool
    can_convert: bool
    is_own: bool
    # Distinct Forth links found in the body, for link-preview cards. Empty for
    # deleted messages (their body is hidden).
    forth_links: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_body(body: str) -> str:
    cleaned = body.strip()
    if not cleaned:
        raise ValidationError("Write a message before sending.", details={"field": "body"})
    if len(cleaned) > MAX_BODY_LENGTH:
        raise ValidationError(
            f"Messages must be at most {MAX_BODY_LENGTH} characters.",
            details={"field": "body"},
        )
    return cleaned


def get_message(db: DbSession, message_id: uuid.UUID) -> Message:
    message = db.get(Message, message_id)
    if message is None:
        raise NotFoundError("Message not found.", code="MESSAGE_NOT_FOUND")
    return message


def get_visible_message(db: DbSession, *, message_id: uuid.UUID, actor: Actor) -> Message:
    message = get_message(db, message_id)
    # Cross-cohort access is a 404 -- the message does not exist for this actor.
    if message.cohort_id != actor.cohort_id:
        raise NotFoundError("Message not found.", code="MESSAGE_NOT_FOUND")
    permissions.require_message_access(db, message, actor)
    return message


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create_message(
    db: DbSession,
    *,
    actor: Actor,
    channel: Channel | None = None,
    conversation: DirectConversation | None = None,
    body: str,
    parent_message_id: uuid.UUID | None = None,
    message_type: MessageType = MessageType.TEXT,
) -> Message:
    """Create a channel message, DM, or thread reply.

    Exactly one destination must be supplied; the database enforces this too.
    """

    if (channel is None) == (conversation is None):
        raise ValidationError(
            "A message must belong to exactly one channel or conversation.",
            code="INVALID_MESSAGE_DESTINATION",
        )

    clean_body = validate_body(body)

    parent: Message | None = None
    if parent_message_id is not None:
        parent = get_message(db, parent_message_id)
        if parent.parent_message_id is not None:
            raise ValidationError(
                "Replies cannot start their own thread.", code="NESTED_THREAD_NOT_ALLOWED"
            )
        if channel is not None and parent.channel_id != channel.id:
            raise ValidationError(
                "That thread belongs to a different channel.", code="THREAD_MISMATCH"
            )
        if conversation is not None and parent.direct_conversation_id != conversation.id:
            raise ValidationError(
                "That thread belongs to a different conversation.", code="THREAD_MISMATCH"
            )

    if channel is not None:
        permissions.require_channel_post(db, channel, actor)
        cohort_id = channel.cohort_id
    else:
        assert conversation is not None
        permissions.require_conversation_member(db, conversation, actor)
        cohort_id = conversation.cohort_id
    message = Message(
        cohort_id=cohort_id,
        sender_id=actor.user_id,
        channel_id=channel.id if channel else None,
        direct_conversation_id=conversation.id if conversation else None,
        parent_message_id=parent.id if parent else None,
        body=clean_body,
        message_type=message_type,
    )
    db.add(message)
    db.flush()

    if parent is not None:
        parent.reply_count = (parent.reply_count or 0) + 1
        parent.last_reply_at = message.created_at
    if conversation is not None:
        conversation.last_message_at = message.created_at

    parsed = mentions.parse(
        db,
        cohort_id=cohort_id,
        body=clean_body,
        author=actor.user,
        channel_id=channel.id if channel else None,
        conversation_id=conversation.id if conversation else None,
    )
    mentions.persist(db, message=message, parsed=parsed)
    _notify_for_message(
        db,
        cohort_id=cohort_id,
        message=message,
        author=actor.user,
        channel=channel,
        conversation=conversation,
        parent=parent,
        mentioned_ids=parsed.user_ids,
    )

    # The author has, by definition, read their own message.
    _advance_read_receipt(db, user_id=actor.user_id, message=message)

    audit.record(
        db,
        AuditAction.MESSAGE_CREATED,
        actor_id=actor.user_id,
        cohort_id=cohort_id,
        entity_type="message",
        entity_id=message.id,
        context={
            "destination": "channel" if channel else "direct",
            "is_reply": parent is not None,
        },
    )
    db.flush()
    return message


def _notify_for_message(
    db: DbSession,
    *,
    cohort_id: uuid.UUID,
    message: Message,
    author: User,
    channel: Channel | None,
    conversation: DirectConversation | None,
    parent: Message | None,
    mentioned_ids: set[uuid.UUID],
) -> None:
    excerpt = message.body[:140]

    if channel is not None:
        link = f"/channels/{channel.slug}#message-{message.id}"
        notifications.create_many(
            db,
            cohort_id=cohort_id,
            recipient_ids=mentioned_ids,
            notification_type=NotificationType.MENTION,
            title=f"{author.display_name} mentioned you in #{channel.name}",
            body=excerpt,
            link_path=link,
            actor_id=author.id,
            message_id=message.id,
            channel_id=channel.id,
        )
    else:
        assert conversation is not None
        link = f"/dm/{conversation.id}#message-{message.id}"
        recipient_ids = [
            member.user_id for member in conversation.members if member.user_id != author.id
        ]
        notifications.create_many(
            db,
            cohort_id=cohort_id,
            recipient_ids=recipient_ids,
            notification_type=NotificationType.DIRECT_MESSAGE,
            title=f"New message from {author.display_name}",
            body=excerpt,
            link_path=link,
            actor_id=author.id,
            message_id=message.id,
            direct_conversation_id=conversation.id,
        )

    if parent is not None:
        thread_participants = set(
            db.scalars(
                select(Message.sender_id).where(
                    (Message.id == parent.id) | (Message.parent_message_id == parent.id)
                )
            ).all()
        )
        thread_participants.discard(author.id)
        thread_participants -= mentioned_ids
        if conversation is not None:
            allowed = {member.user_id for member in conversation.members}
            thread_participants &= allowed
        notifications.create_many(
            db,
            cohort_id=cohort_id,
            recipient_ids=thread_participants,
            notification_type=NotificationType.THREAD_REPLY,
            title=f"{author.display_name} replied in a thread",
            body=excerpt,
            link_path=(
                f"/threads/{parent.id}"
                if channel is None
                else f"/channels/{channel.slug}/threads/{parent.id}"
            ),
            actor_id=author.id,
            message_id=message.id,
        )


# ---------------------------------------------------------------------------
# Edit / delete / pin
# ---------------------------------------------------------------------------


def edit_message(db: DbSession, *, actor: Actor, message: Message, body: str) -> Message:
    permissions.require_message_access(db, message, actor)
    permissions.require_message_edit(message, actor)
    if utcnow() - message.created_at > EDIT_WINDOW:
        raise PermissionDeniedError(
            "Messages can only be edited within 24 hours.", code="EDIT_WINDOW_CLOSED"
        )
    message.body = validate_body(body)
    message.edited_at = utcnow()
    audit.record(
        db,
        AuditAction.MESSAGE_EDITED,
        actor_id=actor.user_id,
        cohort_id=message.cohort_id,
        entity_type="message",
        entity_id=message.id,
    )
    db.flush()
    return message


def soft_delete_message(db: DbSession, *, actor: Actor, message: Message) -> Message:
    """Soft delete: the row stays for audit and thread integrity."""

    permissions.require_message_access(db, message, actor)
    permissions.require_message_delete(message, actor)
    if message.deleted_at is not None:
        return message
    message.deleted_at = utcnow()
    message.deleted_by_id = actor.user_id
    message.is_pinned = False
    audit.record(
        db,
        AuditAction.MESSAGE_DELETED,
        actor_id=actor.user_id,
        cohort_id=message.cohort_id,
        entity_type="message",
        entity_id=message.id,
        context={"by_admin": actor.user_id != message.sender_id},
    )
    db.flush()
    return message


def pin_message(db: DbSession, *, actor: Actor, message: Message) -> Message:
    permissions.require_message_pin(message, actor)
    if message.deleted_at is not None:
        raise ConflictError("Deleted messages cannot be pinned.", code="MESSAGE_DELETED")
    message.is_pinned = True
    message.pinned_at = utcnow()
    message.pinned_by_id = actor.user_id
    audit.record(
        db,
        AuditAction.MESSAGE_PINNED,
        actor_id=actor.user_id,
        cohort_id=message.cohort_id,
        entity_type="message",
        entity_id=message.id,
    )
    db.flush()
    return message


def unpin_message(db: DbSession, *, actor: Actor, message: Message) -> Message:
    permissions.require_message_pin(message, actor)
    message.is_pinned = False
    message.pinned_at = None
    message.pinned_by_id = None
    audit.record(
        db,
        AuditAction.MESSAGE_UNPINNED,
        actor_id=actor.user_id,
        cohort_id=message.cohort_id,
        entity_type="message",
        entity_id=message.id,
    )
    db.flush()
    return message


def list_pinned(db: DbSession, *, channel: Channel, limit: int = 20) -> list[Message]:
    return list(
        db.scalars(
            select(Message)
            .where(
                Message.channel_id == channel.id,
                Message.is_pinned.is_(True),
                Message.deleted_at.is_(None),
            )
            .order_by(Message.pinned_at.desc())
            .limit(limit)
        ).all()
    )


# ---------------------------------------------------------------------------
# Reading: pagination and the polling cursor
# ---------------------------------------------------------------------------


def _base_query(
    *, channel_id: uuid.UUID | None, conversation_id: uuid.UUID | None
) -> Select[tuple[Message]]:
    stmt = select(Message).options(
        selectinload(Message.reactions).selectinload(Reaction.user)
    )
    if channel_id is not None:
        stmt = stmt.where(Message.channel_id == channel_id)
    else:
        stmt = stmt.where(Message.direct_conversation_id == conversation_id)
    # Thread replies are loaded only when a thread is opened.
    return stmt.where(Message.parent_message_id.is_(None))


def list_messages(
    db: DbSession,
    *,
    channel: Channel | None = None,
    conversation: DirectConversation | None = None,
    before_seq: int | None = None,
    limit: int = 50,
) -> list[Message]:
    """Cursor pagination: the newest ``limit`` messages before ``before_seq``."""

    stmt = _base_query(
        channel_id=channel.id if channel else None,
        conversation_id=conversation.id if conversation else None,
    )
    if before_seq is not None:
        stmt = stmt.where(Message.seq < before_seq)
    rows = list(db.scalars(stmt.order_by(Message.seq.desc()).limit(limit)).all())
    rows.reverse()
    return rows


def list_new_messages(
    db: DbSession,
    *,
    channel: Channel | None = None,
    conversation: DirectConversation | None = None,
    after_seq: int,
    limit: int = 100,
) -> list[Message]:
    """The polling endpoint's query: strictly newer messages only."""

    stmt = _base_query(
        channel_id=channel.id if channel else None,
        conversation_id=conversation.id if conversation else None,
    ).where(Message.seq > after_seq)
    return list(db.scalars(stmt.order_by(Message.seq.asc()).limit(limit)).all())


def has_older_messages(
    db: DbSession,
    *,
    channel: Channel | None = None,
    conversation: DirectConversation | None = None,
    oldest_seq: int | None,
) -> bool:
    if oldest_seq is None:
        return False
    stmt = (
        _base_query(
            channel_id=channel.id if channel else None,
            conversation_id=conversation.id if conversation else None,
        )
        .where(Message.seq < oldest_seq)
        .limit(1)
    )
    return db.scalar(select(func.count()).select_from(stmt.subquery())) not in (0, None)


def latest_seq(
    db: DbSession,
    *,
    channel: Channel | None = None,
    conversation: DirectConversation | None = None,
) -> int:
    stmt = select(func.max(Message.seq))
    if channel is not None:
        stmt = stmt.where(Message.channel_id == channel.id)
    else:
        assert conversation is not None
        stmt = stmt.where(Message.direct_conversation_id == conversation.id)
    return int(db.scalar(stmt) or 0)


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


def list_thread_replies(
    db: DbSession, *, parent: Message, limit: int = 200, offset: int = 0
) -> list[Message]:
    """Replies are only ever loaded when a thread is actually opened."""

    return list(
        db.scalars(
            select(Message)
            .options(selectinload(Message.reactions).selectinload(Reaction.user))
            .where(Message.parent_message_id == parent.id)
            .order_by(Message.seq.asc())
            .limit(limit)
            .offset(offset)
        ).all()
    )


def thread_participants(db: DbSession, *, parent: Message) -> list[User]:
    return list(
        db.scalars(
            select(User)
            .where(
                User.id.in_(
                    select(Message.sender_id).where(
                        (Message.id == parent.id) | (Message.parent_message_id == parent.id)
                    )
                )
            )
            .order_by(User.display_name.asc())
        ).all()
    )


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


def add_reaction(
    db: DbSession, *, actor: Actor, message: Message, reaction_type: ReactionType
) -> Reaction:
    permissions.require_message_access(db, message, actor)
    if message.deleted_at is not None:
        raise ConflictError("You cannot react to a deleted message.", code="MESSAGE_DELETED")

    existing = db.scalar(
        select(Reaction).where(
            Reaction.message_id == message.id,
            Reaction.user_id == actor.user_id,
            Reaction.reaction_type == reaction_type,
        )
    )
    if existing is not None:
        return existing

    reaction = Reaction(message_id=message.id, user_id=actor.user_id, reaction_type=reaction_type)
    db.add(reaction)
    try:
        db.flush()
    except IntegrityError:  # unique(message, user, type) -- treat as idempotent
        db.rollback()
        existing = db.scalar(
            select(Reaction).where(
                Reaction.message_id == message.id,
                Reaction.user_id == actor.user_id,
                Reaction.reaction_type == reaction_type,
            )
        )
        if existing is None:
            raise
        return existing
    return reaction


def remove_reaction(
    db: DbSession, *, actor: Actor, message: Message, reaction_type: ReactionType
) -> None:
    permissions.require_message_access(db, message, actor)
    reaction = db.scalar(
        select(Reaction).where(
            Reaction.message_id == message.id,
            Reaction.user_id == actor.user_id,
            Reaction.reaction_type == reaction_type,
        )
    )
    if reaction is None:
        raise NotFoundError("You have not added that reaction.", code="REACTION_NOT_FOUND")
    db.delete(reaction)
    db.flush()


def toggle_reaction(
    db: DbSession, *, actor: Actor, message: Message, reaction_type: ReactionType
) -> bool:
    """Add the reaction if absent, remove it if present. Returns the new state."""

    existing = db.scalar(
        select(Reaction).where(
            Reaction.message_id == message.id,
            Reaction.user_id == actor.user_id,
            Reaction.reaction_type == reaction_type,
        )
    )
    if existing is not None:
        remove_reaction(db, actor=actor, message=message, reaction_type=reaction_type)
        return False
    add_reaction(db, actor=actor, message=message, reaction_type=reaction_type)
    return True


def summarize_reactions(message: Message, *, viewer_id: uuid.UUID) -> list[ReactionSummary]:
    """Group a message's reactions for display, in a stable order."""

    buckets: dict[ReactionType, ReactionSummary] = {}
    for reaction in message.reactions:
        summary = buckets.get(reaction.reaction_type)
        if summary is None:
            summary = ReactionSummary(
                reaction_type=reaction.reaction_type, count=0, reacted=False
            )
            buckets[reaction.reaction_type] = summary
        summary.count += 1
        if reaction.user_id == viewer_id:
            summary.reacted = True
        if len(summary.participants) < 12:
            summary.participants.append(reaction.user.display_name)
    return [buckets[t] for t in ReactionType if t in buckets]


def build_view(db: DbSession, message: Message, *, viewer: Actor) -> MessageView:
    """Assemble the server-computed view model handed to templates."""

    return MessageView(
        message=message,
        reactions=summarize_reactions(message, viewer_id=viewer.user_id),
        can_edit=permissions.can_edit_message(message, viewer)
        and utcnow() - message.created_at <= EDIT_WINDOW,
        can_delete=message.deleted_at is None
        and permissions.can_delete_message(message, viewer),
        can_pin=message.deleted_at is None and permissions.can_pin_message(message, viewer),
        can_convert=message.deleted_at is None
        and permissions.can_convert_message(message),
        is_own=message.sender_id == viewer.user_id,
        forth_links=(
            forth.extract_forth_links(message.body) if message.deleted_at is None else []
        ),
    )


def build_views(db: DbSession, messages: list[Message], *, viewer: Actor) -> list[MessageView]:
    return [build_view(db, message, viewer=viewer) for message in messages]


# ---------------------------------------------------------------------------
# Read receipts and unread counts
# ---------------------------------------------------------------------------


def _get_receipt(
    db: DbSession,
    *,
    user_id: uuid.UUID,
    channel_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
) -> ReadReceipt | None:
    stmt = select(ReadReceipt).where(ReadReceipt.user_id == user_id)
    if channel_id is not None:
        stmt = stmt.where(ReadReceipt.channel_id == channel_id)
    else:
        stmt = stmt.where(ReadReceipt.direct_conversation_id == conversation_id)
    return db.scalar(stmt)


def _advance_read_receipt(db: DbSession, *, user_id: uuid.UUID, message: Message) -> ReadReceipt:
    receipt = _get_receipt(
        db,
        user_id=user_id,
        channel_id=message.channel_id,
        conversation_id=message.direct_conversation_id,
    )
    if receipt is None:
        receipt = ReadReceipt(
            user_id=user_id,
            channel_id=message.channel_id,
            direct_conversation_id=message.direct_conversation_id,
            last_read_message_id=message.id,
            last_read_seq=message.seq,
            last_read_at=utcnow(),
        )
        db.add(receipt)
    elif message.seq > receipt.last_read_seq:
        receipt.last_read_message_id = message.id
        receipt.last_read_seq = message.seq
        receipt.last_read_at = utcnow()
    db.flush()
    return receipt


def update_read_receipt(
    db: DbSession,
    *,
    actor: Actor,
    channel: Channel | None = None,
    conversation: DirectConversation | None = None,
    last_read_message_id: uuid.UUID | None = None,
) -> ReadReceipt | None:
    """Mark a channel/conversation read up to a message (or its newest message)."""

    if (channel is None) == (conversation is None):
        raise ValidationError(
            "A read receipt needs exactly one channel or conversation.",
            code="INVALID_READ_SCOPE",
        )
    if conversation is not None:
        permissions.require_conversation_member(db, conversation, actor)

    if last_read_message_id is not None:
        message = get_message(db, last_read_message_id)
        if channel is not None and message.channel_id != channel.id:
            raise ValidationError("That message is not in this channel.", code="SCOPE_MISMATCH")
        if conversation is not None and message.direct_conversation_id != conversation.id:
            raise ValidationError(
                "That message is not in this conversation.", code="SCOPE_MISMATCH"
            )
        return _advance_read_receipt(db, user_id=actor.user_id, message=message)

    newest_seq = latest_seq(db, channel=channel, conversation=conversation)
    if newest_seq == 0:
        return None
    newest = db.scalar(select(Message).where(Message.seq == newest_seq))
    if newest is None:
        return None
    return _advance_read_receipt(db, user_id=actor.user_id, message=newest)


def unread_count_for_channel(
    db: DbSession, *, user_id: uuid.UUID, channel_id: uuid.UUID
) -> int:
    receipt = _get_receipt(db, user_id=user_id, channel_id=channel_id, conversation_id=None)
    last_seq = receipt.last_read_seq if receipt else 0
    return int(
        db.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.channel_id == channel_id,
                Message.seq > last_seq,
                Message.deleted_at.is_(None),
                Message.sender_id != user_id,
            )
        )
        or 0
    )


def unread_count_for_conversation(
    db: DbSession, *, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> int:
    receipt = _get_receipt(
        db, user_id=user_id, channel_id=None, conversation_id=conversation_id
    )
    last_seq = receipt.last_read_seq if receipt else 0
    return int(
        db.scalar(
            select(func.count())
            .select_from(Message)
            .where(
                Message.direct_conversation_id == conversation_id,
                Message.seq > last_seq,
                Message.deleted_at.is_(None),
                Message.sender_id != user_id,
            )
        )
        or 0
    )


def total_unread(db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID) -> int:
    """Total unread across joined channels and every conversation, computed in SQL."""

    channel_receipts = (
        select(ReadReceipt.channel_id, ReadReceipt.last_read_seq)
        .where(ReadReceipt.user_id == user_id, ReadReceipt.channel_id.is_not(None))
        .subquery()
    )
    channel_unread = (
        select(func.count())
        .select_from(Message)
        .join(ChannelMember, ChannelMember.channel_id == Message.channel_id)
        .join(
            channel_receipts,
            channel_receipts.c.channel_id == Message.channel_id,
            isouter=True,
        )
        .where(
            ChannelMember.user_id == user_id,
            Message.cohort_id == cohort_id,
            Message.deleted_at.is_(None),
            Message.sender_id != user_id,
            Message.seq > func.coalesce(channel_receipts.c.last_read_seq, 0),
        )
    )

    conversation_receipts = (
        select(ReadReceipt.direct_conversation_id, ReadReceipt.last_read_seq)
        .where(
            ReadReceipt.user_id == user_id, ReadReceipt.direct_conversation_id.is_not(None)
        )
        .subquery()
    )
    from app.models.channel import DirectConversationMember

    dm_unread = (
        select(func.count())
        .select_from(Message)
        .join(
            DirectConversationMember,
            DirectConversationMember.conversation_id == Message.direct_conversation_id,
        )
        .join(
            conversation_receipts,
            conversation_receipts.c.direct_conversation_id == Message.direct_conversation_id,
            isouter=True,
        )
        .where(
            DirectConversationMember.user_id == user_id,
            Message.cohort_id == cohort_id,
            Message.deleted_at.is_(None),
            Message.sender_id != user_id,
            Message.seq > func.coalesce(conversation_receipts.c.last_read_seq, 0),
        )
    )

    return int(db.scalar(channel_unread) or 0) + int(db.scalar(dm_unread) or 0)


def message_count(db: DbSession) -> int:
    return int(db.scalar(select(func.count()).select_from(Message)) or 0)
