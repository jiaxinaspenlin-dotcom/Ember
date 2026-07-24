"""Mention parsing.

Mentions are parsed **in Python** on the server.  The browser never decides who
gets notified: the parser resolves handles against the database and then filters
the result through the same access rules used everywhere else.

Supported forms::

    @display-name   -> a specific member
    @channel        -> every member of the channel
    @admins         -> every administrator
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.core.enums import MentionType, UserRole
from app.models.channel import ChannelMember, DirectConversationMember
from app.models.cohort import CohortMembership
from app.models.message import Mention, Message
from app.models.user import User

# A handle is the member's display name with spaces collapsed to hyphens or
# written directly; we accept letters, digits, dot, underscore and hyphen.
MENTION_PATTERN = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9._-]{0,63})")

CHANNEL_KEYWORD = "channel"
ADMINS_KEYWORD = "admins"


@dataclass(slots=True)
class ParsedMentions:
    """The result of parsing a message body."""

    raw_handles: list[str] = field(default_factory=list)
    mentions_channel: bool = False
    mentions_admins: bool = False
    user_ids: set[uuid.UUID] = field(default_factory=set)
    unresolved: list[str] = field(default_factory=list)


def extract_handles(body: str) -> list[str]:
    """Pure text extraction -- no database access, easy to test."""

    seen: set[str] = set()
    handles: list[str] = []
    for match in MENTION_PATTERN.finditer(body or ""):
        handle = match.group(1).rstrip(".")
        key = handle.lower()
        if key in seen:
            continue
        seen.add(key)
        handles.append(handle)
    return handles


def normalize_handle(handle: str) -> str:
    """Normalise a handle for matching against display names."""

    return re.sub(r"[\s_-]+", "", handle.strip().lower())


def parse(
    db: DbSession,
    *,
    cohort_id: uuid.UUID,
    body: str,
    author: User,
    channel_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
) -> ParsedMentions:
    """Resolve mention handles to real, *authorised* users in the cohort."""

    result = ParsedMentions()
    handles = extract_handles(body)
    result.raw_handles = handles
    if not handles:
        return result

    audience = _audience_ids(db, channel_id=channel_id, conversation_id=conversation_id)

    person_handles: list[str] = []
    for handle in handles:
        lowered = handle.lower()
        if lowered == CHANNEL_KEYWORD:
            result.mentions_channel = True
        elif lowered == ADMINS_KEYWORD:
            result.mentions_admins = True
        else:
            person_handles.append(handle)

    if result.mentions_channel:
        result.user_ids |= audience

    if result.mentions_admins:
        # Admins are per-cohort now: an @admins ping reaches the admins of this
        # cohort only, never admins of any other cohort.
        admin_ids = set(
            db.scalars(
                select(CohortMembership.user_id)
                .join(User, User.id == CohortMembership.user_id)
                .where(
                    CohortMembership.cohort_id == cohort_id,
                    CohortMembership.role == UserRole.ADMIN,
                    User.is_active.is_(True),
                )
            ).all()
        )
        # @admins in a DM only reaches admins who are in that conversation.
        result.user_ids |= admin_ids & audience if conversation_id else admin_ids

    if person_handles:
        normalized = {normalize_handle(h): h for h in person_handles}
        candidates = db.execute(
            select(User.id, User.display_name).where(User.is_active.is_(True))
        ).all()
        matched_keys: set[str] = set()
        for user_id, display_name in candidates:
            key = normalize_handle(display_name)
            if key in normalized:
                matched_keys.add(key)
                # Never notify someone who cannot see the message.
                if user_id in audience:
                    result.user_ids.add(user_id)
        result.unresolved = [
            original for key, original in normalized.items() if key not in matched_keys
        ]

    # A member never notifies themselves.
    result.user_ids.discard(author.id)
    return result


def _audience_ids(
    db: DbSession,
    *,
    channel_id: uuid.UUID | None,
    conversation_id: uuid.UUID | None,
) -> set[uuid.UUID]:
    """Everyone entitled to read messages in this destination."""

    if conversation_id is not None:
        return set(
            db.scalars(
                select(DirectConversationMember.user_id).where(
                    DirectConversationMember.conversation_id == conversation_id
                )
            ).all()
        )
    if channel_id is not None:
        return set(
            db.scalars(
                select(ChannelMember.user_id).where(ChannelMember.channel_id == channel_id)
            ).all()
        )
    return set()


def persist(db: DbSession, *, message: Message, parsed: ParsedMentions) -> list[Mention]:
    """Store mention rows for a freshly created message."""

    rows: list[Mention] = []
    if parsed.mentions_channel:
        rows.append(
            Mention(
                message_id=message.id, mention_type=MentionType.CHANNEL, raw_text="@channel"
            )
        )
    if parsed.mentions_admins:
        rows.append(
            Mention(message_id=message.id, mention_type=MentionType.ADMINS, raw_text="@admins")
        )
    for user_id in sorted(parsed.user_ids, key=str):
        rows.append(
            Mention(
                message_id=message.id,
                mention_type=MentionType.USER,
                mentioned_user_id=user_id,
                raw_text="@mention",
            )
        )
    for row in rows:
        db.add(row)
    if rows:
        db.flush()
    return rows


def recent_mentions_for_user(
    db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID, limit: int = 5
) -> list[Message]:
    """Recent messages in this cohort that mention this user, newest first."""

    return list(
        db.scalars(
            select(Message)
            .join(Mention, Mention.message_id == Message.id)
            .where(
                Message.cohort_id == cohort_id,
                Mention.mentioned_user_id == user_id,
                Message.deleted_at.is_(None),
                Message.sender_id != user_id,
            )
            .order_by(Message.created_at.desc())
            .limit(limit)
        ).all()
    )


def mention_count_for_user(db: DbSession, *, user_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Mention)
            .where(Mention.mentioned_user_id == user_id)
        )
        or 0
    )
