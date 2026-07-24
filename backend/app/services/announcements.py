"""Administrator announcements."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.auth import permissions
from app.core.enums import AuditAction, NotificationType, Priority
from app.core.errors import NotFoundError, ValidationError
from app.db.base import utcnow
from app.models.cohort import Cohort, CohortMembership
from app.models.engagement import Announcement
from app.services import audit, notifications

Actor = CohortMembership


def get_announcement(
    db: DbSession, cohort: Cohort, announcement_id: uuid.UUID
) -> Announcement:
    announcement = db.get(Announcement, announcement_id)
    if announcement is None or announcement.cohort_id != cohort.id:
        raise NotFoundError("Announcement not found.", code="ANNOUNCEMENT_NOT_FOUND")
    return announcement


def create_announcement(
    db: DbSession,
    *,
    author: Actor,
    title: str,
    body: str,
    priority: Priority = Priority.NORMAL,
    expires_at: dt.datetime | None = None,
    is_pinned: bool = False,
    notify_everyone: bool = True,
) -> Announcement:
    permissions.require_announcement_management(author)

    clean_title = " ".join(title.split())
    if len(clean_title) < 4:
        raise ValidationError(
            "Give the announcement a title of at least 4 characters.",
            details={"field": "title"},
        )
    clean_body = body.strip()
    if not clean_body:
        raise ValidationError("Write the announcement body.", details={"field": "body"})

    announcement = Announcement(
        cohort_id=author.cohort_id,
        title=clean_title[:200],
        body=clean_body,
        author_id=author.user_id,
        priority=priority,
        published_at=utcnow(),
        expires_at=expires_at,
        is_pinned=is_pinned,
    )
    db.add(announcement)
    db.flush()

    if notify_everyone:
        from app.models.cohort import CohortMembership as _CM

        recipient_ids = list(
            db.scalars(
                select(_CM.user_id).where(
                    _CM.cohort_id == author.cohort_id, _CM.user_id != author.user_id
                )
            ).all()
        )
        notifications.create_many(
            db,
            cohort_id=author.cohort_id,
            recipient_ids=recipient_ids,
            notification_type=NotificationType.ANNOUNCEMENT,
            title=f"Announcement: {announcement.title}",
            body=clean_body[:200],
            link_path=f"/announcements#announcement-{announcement.id}",
            actor_id=author.user_id,
            announcement_id=announcement.id,
        )

    audit.record(
        db,
        AuditAction.ANNOUNCEMENT_CREATED,
        actor_id=author.user_id,
        cohort_id=author.cohort_id,
        entity_type="announcement",
        entity_id=announcement.id,
        context={"priority": priority.value},
    )
    db.flush()
    return announcement


def update_announcement(
    db: DbSession,
    *,
    actor: Actor,
    announcement: Announcement,
    title: str | None = None,
    body: str | None = None,
    priority: Priority | None = None,
    expires_at: dt.datetime | None = None,
    clear_expiry: bool = False,
    is_pinned: bool | None = None,
) -> Announcement:
    permissions.require_announcement_management(actor)
    if title is not None:
        clean = " ".join(title.split())
        if len(clean) < 4:
            raise ValidationError(
                "Give the announcement a title of at least 4 characters.",
                details={"field": "title"},
            )
        announcement.title = clean[:200]
    if body is not None:
        cleaned = body.strip()
        if not cleaned:
            raise ValidationError("Write the announcement body.", details={"field": "body"})
        announcement.body = cleaned
    if priority is not None:
        announcement.priority = priority
    if clear_expiry:
        announcement.expires_at = None
    elif expires_at is not None:
        announcement.expires_at = expires_at
    if is_pinned is not None:
        announcement.is_pinned = is_pinned
    audit.record(
        db,
        AuditAction.ANNOUNCEMENT_UPDATED,
        actor_id=actor.user_id,
        cohort_id=announcement.cohort_id,
        entity_type="announcement",
        entity_id=announcement.id,
    )
    db.flush()
    return announcement


def delete_announcement(db: DbSession, *, actor: Actor, announcement: Announcement) -> None:
    permissions.require_announcement_management(actor)
    audit.record(
        db,
        AuditAction.ANNOUNCEMENT_DELETED,
        actor_id=actor.user_id,
        cohort_id=announcement.cohort_id,
        entity_type="announcement",
        entity_id=announcement.id,
        context={"title": announcement.title},
    )
    db.delete(announcement)
    db.flush()


def list_announcements(
    db: DbSession,
    *,
    cohort: Cohort,
    include_expired: bool = False,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[Announcement], int]:
    stmt = select(Announcement).where(Announcement.cohort_id == cohort.id)
    if not include_expired:
        stmt = stmt.where(
            or_(Announcement.expires_at.is_(None), Announcement.expires_at > utcnow())
        )
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    items = list(
        db.scalars(
            stmt.order_by(
                Announcement.is_pinned.desc(), Announcement.published_at.desc()
            )
            .limit(limit)
            .offset(offset)
        ).all()
    )
    return items, total


def recent_announcements(db: DbSession, *, cohort: Cohort, limit: int = 3) -> list[Announcement]:
    items, _ = list_announcements(db, cohort=cohort, limit=limit)
    return items
