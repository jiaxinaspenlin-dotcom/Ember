"""Persisted, private notifications.

Every notification belongs to exactly one recipient and is only ever readable by
that recipient.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DbSession

from app.core.enums import NotificationType
from app.core.errors import NotFoundError
from app.db.base import rows_affected, utcnow
from app.models.engagement import Notification
from app.models.user import User


def create_notification(
    db: DbSession,
    *,
    cohort_id: uuid.UUID,
    recipient_id: uuid.UUID,
    notification_type: NotificationType,
    title: str,
    link_path: str,
    body: str | None = None,
    actor_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
    channel_id: uuid.UUID | None = None,
    direct_conversation_id: uuid.UUID | None = None,
    help_request_id: uuid.UUID | None = None,
    decision_id: uuid.UUID | None = None,
    task_id: uuid.UUID | None = None,
    announcement_id: uuid.UUID | None = None,
) -> Notification | None:
    """Create one notification. Self-notifications are skipped."""

    if actor_id is not None and actor_id == recipient_id:
        return None
    notification = Notification(
        cohort_id=cohort_id,
        recipient_id=recipient_id,
        actor_id=actor_id,
        notification_type=notification_type,
        title=title[:200],
        body=(body or "")[:500] or None,
        link_path=link_path[:300],
        message_id=message_id,
        channel_id=channel_id,
        direct_conversation_id=direct_conversation_id,
        help_request_id=help_request_id,
        decision_id=decision_id,
        task_id=task_id,
        announcement_id=announcement_id,
    )
    db.add(notification)
    return notification


def create_many(
    db: DbSession,
    *,
    cohort_id: uuid.UUID,
    recipient_ids: Iterable[uuid.UUID],
    notification_type: NotificationType,
    title: str,
    link_path: str,
    body: str | None = None,
    actor_id: uuid.UUID | None = None,
    **references: uuid.UUID | None,
) -> list[Notification]:
    created: list[Notification] = []
    for recipient_id in recipient_ids:
        notification = create_notification(
            db,
            cohort_id=cohort_id,
            recipient_id=recipient_id,
            notification_type=notification_type,
            title=title,
            link_path=link_path,
            body=body,
            actor_id=actor_id,
            **references,
        )
        if notification is not None:
            created.append(notification)
    if created:
        db.flush()
    return created


def list_for_user(
    db: DbSession,
    *,
    cohort_id: uuid.UUID,
    user: User,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[Notification], int]:
    stmt = select(Notification).where(
        Notification.cohort_id == cohort_id, Notification.recipient_id == user.id
    )
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    items = list(
        db.scalars(
            stmt.order_by(Notification.created_at.desc()).limit(limit).offset(offset)
        ).all()
    )
    return items, total


def unread_count(db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Notification)
            .where(
                Notification.cohort_id == cohort_id,
                Notification.recipient_id == user_id,
                Notification.read_at.is_(None),
            )
        )
        or 0
    )


def mark_notification_read(
    db: DbSession, *, user: User, notification_id: uuid.UUID
) -> Notification:
    notification = db.get(Notification, notification_id)
    # A notification belonging to someone else must not even be acknowledged.
    if notification is None or notification.recipient_id != user.id:
        raise NotFoundError("Notification not found.", code="NOTIFICATION_NOT_FOUND")
    if notification.read_at is None:
        notification.read_at = utcnow()
        db.flush()
    return notification


def mark_all_read(db: DbSession, *, cohort_id: uuid.UUID, user: User) -> int:
    result = db.execute(
        update(Notification)
        .where(
            Notification.cohort_id == cohort_id,
            Notification.recipient_id == user.id,
            Notification.read_at.is_(None),
        )
        .values(read_at=utcnow())
    )
    db.flush()
    return rows_affected(result)
