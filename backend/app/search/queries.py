"""Permission-aware search built on PostgreSQL full-text search.

The permission filter is applied *inside the SQL query*, not after the fact, so
a user can never receive:

* direct messages from conversations they do not participate in,
* soft-deleted content,
* anyone else's private notifications.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy import ColumnElement, Select, and_, func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.models.action import Decision, HelpRequest
from app.models.channel import Channel, DirectConversationMember
from app.models.engagement import Announcement
from app.models.message import Message
from app.models.user import User

MAX_RESULTS = 100
EXCERPT_RADIUS = 90


class SearchScope(StrEnum):
    ALL = "all"
    MESSAGES = "messages"
    HELP_REQUESTS = "help_requests"
    DECISIONS = "decisions"
    ANNOUNCEMENTS = "announcements"


@dataclass(slots=True)
class SearchFilters:
    query: str
    scope: SearchScope = SearchScope.ALL
    channel_id: uuid.UUID | None = None
    sender_id: uuid.UUID | None = None
    date_from: dt.datetime | None = None
    date_to: dt.datetime | None = None
    include_direct_messages: bool = True


@dataclass(slots=True)
class SearchResult:
    kind: str
    id: uuid.UUID
    title: str
    excerpt: str
    source_label: str
    link_path: str
    author_name: str | None
    created_at: dt.datetime


@dataclass(slots=True)
class SearchResponse:
    results: list[SearchResult] = field(default_factory=list)
    total: int = 0
    limit: int = 25
    offset: int = 0

    @property
    def has_more(self) -> bool:
        return self.offset + len(self.results) < self.total


def normalize_query(raw: str) -> str:
    """Trim and collapse the user's query. Empty queries are rejected upstream."""

    return " ".join((raw or "").split())[:200]


def build_excerpt(body: str, query: str) -> str:
    """Return a short window of ``body`` around the first matching term."""

    text = " ".join((body or "").split())
    terms = [t for t in re.split(r"\W+", query.lower()) if len(t) > 1]
    lowered = text.lower()
    position = -1
    for term in terms:
        position = lowered.find(term)
        if position != -1:
            break
    if position == -1:
        return text[: EXCERPT_RADIUS * 2] + ("…" if len(text) > EXCERPT_RADIUS * 2 else "")
    start = max(0, position - EXCERPT_RADIUS)
    end = min(len(text), position + EXCERPT_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def _visible_messages_filter(user_id: uuid.UUID, include_dms: bool) -> ColumnElement[bool]:
    """The SQL permission boundary for message search."""

    channel_visible = Message.channel_id.is_not(None)
    if not include_dms:
        return and_(channel_visible, Message.deleted_at.is_(None))
    dm_visible = Message.direct_conversation_id.in_(
        select(DirectConversationMember.conversation_id).where(
            DirectConversationMember.user_id == user_id
        )
    )
    return and_(or_(channel_visible, dm_visible), Message.deleted_at.is_(None))


def _message_query(
    filters: SearchFilters, *, cohort_id: uuid.UUID, user_id: uuid.UUID
) -> Select[tuple[Message]]:
    tsquery = func.websearch_to_tsquery("english", filters.query)
    stmt = select(Message).where(
        Message.cohort_id == cohort_id,
        _visible_messages_filter(user_id, filters.include_direct_messages),
        or_(
            Message.search_vector.op("@@")(tsquery),
            func.lower(Message.body).like(f"%{filters.query.lower()}%"),
        ),
    )
    if filters.channel_id is not None:
        stmt = stmt.where(Message.channel_id == filters.channel_id)
    if filters.sender_id is not None:
        stmt = stmt.where(Message.sender_id == filters.sender_id)
    if filters.date_from is not None:
        stmt = stmt.where(Message.created_at >= filters.date_from)
    if filters.date_to is not None:
        stmt = stmt.where(Message.created_at <= filters.date_to)
    return stmt


def search(
    db: DbSession,
    *,
    cohort_id: uuid.UUID,
    user: User,
    filters: SearchFilters,
    limit: int = 25,
    offset: int = 0,
) -> SearchResponse:
    """Run the search. Results are always capped and paginated."""

    limit = max(1, min(limit, MAX_RESULTS))
    query = normalize_query(filters.query)
    if not query:
        return SearchResponse(results=[], total=0, limit=limit, offset=offset)
    filters.query = query

    results: list[SearchResult] = []
    total = 0

    if filters.scope in (SearchScope.ALL, SearchScope.MESSAGES):
        stmt = _message_query(filters, cohort_id=cohort_id, user_id=user.id)
        total += int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
        rows = db.scalars(
            stmt.order_by(Message.created_at.desc()).limit(limit).offset(offset)
        ).all()
        results.extend(_message_result(db, message, query) for message in rows)

    if filters.scope in (SearchScope.ALL, SearchScope.HELP_REQUESTS):
        pattern = f"%{query.lower()}%"
        stmt_help = select(HelpRequest).where(
            HelpRequest.cohort_id == cohort_id,
            or_(
                func.lower(HelpRequest.title).like(pattern),
                func.lower(HelpRequest.description).like(pattern),
            )
        )
        if filters.sender_id is not None:
            stmt_help = stmt_help.where(HelpRequest.requester_id == filters.sender_id)
        if filters.channel_id is not None:
            stmt_help = stmt_help.where(HelpRequest.source_channel_id == filters.channel_id)
        total += int(db.scalar(select(func.count()).select_from(stmt_help.subquery())) or 0)
        for help_request in db.scalars(
            stmt_help.order_by(HelpRequest.created_at.desc()).limit(limit).offset(offset)
        ).all():
            results.append(
                SearchResult(
                    kind="help_request",
                    id=help_request.id,
                    title=help_request.title,
                    excerpt=build_excerpt(help_request.description, query),
                    source_label=f"Help queue · {help_request.status.label}",
                    link_path=f"/help/{help_request.id}",
                    author_name=help_request.requester.display_name,
                    created_at=help_request.created_at,
                )
            )

    if filters.scope in (SearchScope.ALL, SearchScope.DECISIONS):
        tsquery = func.websearch_to_tsquery("english", query)
        pattern = f"%{query.lower()}%"
        stmt_decision = select(Decision).where(
            Decision.cohort_id == cohort_id,
            or_(
                Decision.search_vector.op("@@")(tsquery),
                func.lower(Decision.title).like(pattern),
            )
        )
        if filters.sender_id is not None:
            stmt_decision = stmt_decision.where(Decision.author_id == filters.sender_id)
        if filters.channel_id is not None:
            stmt_decision = stmt_decision.where(
                Decision.source_channel_id == filters.channel_id
            )
        total += int(db.scalar(select(func.count()).select_from(stmt_decision.subquery())) or 0)
        for decision in db.scalars(
            stmt_decision.order_by(Decision.created_at.desc()).limit(limit).offset(offset)
        ).all():
            results.append(
                SearchResult(
                    kind="decision",
                    id=decision.id,
                    title=decision.title,
                    excerpt=build_excerpt(decision.decision_text, query),
                    source_label=f"Decision log · {decision.status.label}",
                    link_path=f"/decisions/{decision.id}",
                    author_name=decision.author.display_name,
                    created_at=decision.created_at,
                )
            )

    if filters.scope in (SearchScope.ALL, SearchScope.ANNOUNCEMENTS):
        pattern = f"%{query.lower()}%"
        stmt_ann = select(Announcement).where(
            Announcement.cohort_id == cohort_id,
            or_(
                func.lower(Announcement.title).like(pattern),
                func.lower(Announcement.body).like(pattern),
            )
        )
        total += int(db.scalar(select(func.count()).select_from(stmt_ann.subquery())) or 0)
        for announcement in db.scalars(
            stmt_ann.order_by(Announcement.published_at.desc()).limit(limit).offset(offset)
        ).all():
            results.append(
                SearchResult(
                    kind="announcement",
                    id=announcement.id,
                    title=announcement.title,
                    excerpt=build_excerpt(announcement.body, query),
                    source_label="Announcement",
                    link_path=f"/announcements#announcement-{announcement.id}",
                    author_name=announcement.author.display_name,
                    created_at=announcement.published_at,
                )
            )

    results.sort(key=lambda item: item.created_at, reverse=True)
    return SearchResponse(
        results=results[:limit], total=total, limit=limit, offset=offset
    )


def _message_result(db: DbSession, message: Message, query: str) -> SearchResult:
    if message.channel_id is not None:
        channel = db.get(Channel, message.channel_id)
        source_label = f"#{channel.name}" if channel else "Channel"
        link = (
            f"/channels/{channel.slug}#message-{message.id}"
            if channel
            else f"/messages/{message.id}"
        )
    else:
        source_label = "Direct message"
        link = f"/dm/{message.direct_conversation_id}#message-{message.id}"
    return SearchResult(
        kind="message",
        id=message.id,
        title=message.sender.display_name,
        excerpt=build_excerpt(message.body, query),
        source_label=source_label,
        link_path=link,
        author_name=message.sender.display_name,
        created_at=message.created_at,
    )
