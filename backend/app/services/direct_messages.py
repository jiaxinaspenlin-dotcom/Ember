"""Private one-to-one conversations.

Membership is validated on *every* read and write.  A non-participant receives a
404 -- the existence of a conversation is itself private.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.auth import permissions
from app.core.errors import NotFoundError, ValidationError
from app.models.channel import DirectConversation, DirectConversationMember
from app.models.cohort import Cohort, CohortMembership
from app.models.message import Message, ReadReceipt
from app.models.user import User
from app.services import accounts, cohorts

Actor = CohortMembership


@dataclass(slots=True)
class ConversationListItem:
    conversation: DirectConversation
    other_member: User
    unread_count: int
    last_message: Message | None


def get_conversation(
    db: DbSession, conversation_id: uuid.UUID, *, actor: Actor
) -> DirectConversation:
    conversation = db.get(DirectConversation, conversation_id)
    if conversation is None or conversation.cohort_id != actor.cohort_id:
        raise NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND")
    permissions.require_conversation_member(db, conversation, actor)
    return conversation


def find_conversation_between(
    db: DbSession, *, cohort_id: uuid.UUID, user_a: uuid.UUID, user_b: uuid.UUID
) -> DirectConversation | None:
    return db.scalar(
        select(DirectConversation).where(
            DirectConversation.cohort_id == cohort_id,
            DirectConversation.pair_key == DirectConversation.build_pair_key(user_a, user_b),
        )
    )


def get_or_create_conversation(
    db: DbSession, *, actor: Actor, other_user_id: uuid.UUID
) -> DirectConversation:
    """Open (or reopen) the single conversation between two cohort members."""

    if other_user_id == actor.user_id:
        raise ValidationError(
            "You cannot start a conversation with yourself.", code="SELF_CONVERSATION"
        )
    other = accounts.require_user(db, other_user_id)
    # The other person must be in the same cohort.
    if cohorts.get_membership(db, cohort_id=actor.cohort_id, user_id=other.id) is None:
        raise NotFoundError("Member not found.", code="USER_NOT_FOUND")

    existing = find_conversation_between(
        db, cohort_id=actor.cohort_id, user_a=actor.user_id, user_b=other.id
    )
    if existing is not None:
        return existing

    conversation = DirectConversation(
        cohort_id=actor.cohort_id,
        pair_key=DirectConversation.build_pair_key(actor.user_id, other.id),
        created_by_id=actor.user_id,
    )
    db.add(conversation)
    db.flush()
    db.add(DirectConversationMember(conversation_id=conversation.id, user_id=actor.user_id))
    db.add(DirectConversationMember(conversation_id=conversation.id, user_id=other.id))
    try:
        db.flush()
    except IntegrityError:  # pragma: no cover - concurrent creation
        db.rollback()
        existing = find_conversation_between(
            db, cohort_id=actor.cohort_id, user_a=actor.user_id, user_b=other.id
        )
        if existing is None:
            raise
        return existing
    return conversation


def other_member(conversation: DirectConversation, *, user_id: uuid.UUID) -> User:
    for member in conversation.members:
        if member.user_id != user_id:
            return member.user
    # A conversation always has two members; this only happens if the other
    # account was deleted.
    raise NotFoundError("The other participant is no longer available.", code="MEMBER_MISSING")


def list_conversations(
    db: DbSession, *, cohort: Cohort, user: User, limit: int = 50, offset: int = 0
) -> tuple[list[ConversationListItem], int]:
    """The user's conversations with unread counts, computed in SQL."""

    receipts = (
        select(ReadReceipt.direct_conversation_id, ReadReceipt.last_read_seq)
        .where(ReadReceipt.user_id == user.id, ReadReceipt.direct_conversation_id.is_not(None))
        .subquery()
    )
    unread = (
        select(
            Message.direct_conversation_id.label("conversation_id"),
            func.count().label("unread_count"),
        )
        .join(
            receipts,
            receipts.c.direct_conversation_id == Message.direct_conversation_id,
            isouter=True,
        )
        .where(
            Message.direct_conversation_id.is_not(None),
            Message.deleted_at.is_(None),
            Message.sender_id != user.id,
            Message.seq > func.coalesce(receipts.c.last_read_seq, 0),
        )
        .group_by(Message.direct_conversation_id)
        .subquery()
    )

    stmt = (
        select(DirectConversation, func.coalesce(unread.c.unread_count, 0))
        .join(
            DirectConversationMember,
            DirectConversationMember.conversation_id == DirectConversation.id,
        )
        .join(unread, unread.c.conversation_id == DirectConversation.id, isouter=True)
        .where(
            DirectConversation.cohort_id == cohort.id,
            DirectConversationMember.user_id == user.id,
        )
        .options(
            selectinload(DirectConversation.members).selectinload(
                DirectConversationMember.user
            )
        )
    )

    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = db.execute(
        stmt.order_by(
            func.coalesce(DirectConversation.last_message_at, DirectConversation.created_at).desc()
        )
        .limit(limit)
        .offset(offset)
    ).all()

    items: list[ConversationListItem] = []
    for conversation, unread_count in rows:
        try:
            other = other_member(conversation, user_id=user.id)
        except NotFoundError:
            continue
        items.append(
            ConversationListItem(
                conversation=conversation,
                other_member=other,
                unread_count=int(unread_count or 0),
                last_message=_last_message(db, conversation.id),
            )
        )
    return items, total


def _last_message(db: DbSession, conversation_id: uuid.UUID) -> Message | None:
    return db.scalar(
        select(Message)
        .where(
            Message.direct_conversation_id == conversation_id,
            Message.parent_message_id.is_(None),
        )
        .order_by(Message.seq.desc())
        .limit(1)
    )


def conversation_count(db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(DirectConversationMember)
            .join(
                DirectConversation,
                DirectConversation.id == DirectConversationMember.conversation_id,
            )
            .where(
                DirectConversationMember.user_id == user_id,
                DirectConversation.cohort_id == cohort_id,
            )
        )
        or 0
    )
