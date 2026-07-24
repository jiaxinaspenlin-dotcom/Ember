"""Audit logging.

Audit rows are written in the same transaction as the action they describe, so
an action and its audit record either both persist or neither does.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession

from app.core.enums import AuditAction
from app.core.logging import scrub
from app.models.engagement import AuditEvent


def record(
    db: DbSession,
    action: AuditAction,
    *,
    actor_id: uuid.UUID | None = None,
    cohort_id: uuid.UUID | None = None,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    context: dict[str, Any] | None = None,
    ip_address: str | None = None,
) -> AuditEvent:
    event = AuditEvent(
        actor_id=actor_id,
        cohort_id=cohort_id,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        context=scrub(context) if context else None,
        ip_address=(ip_address or "")[:64] or None,
    )
    db.add(event)
    return event


def recent_events(
    db: DbSession, *, cohort_id: uuid.UUID | None = None, limit: int = 50, offset: int = 0
) -> list[AuditEvent]:
    stmt = select(AuditEvent)
    if cohort_id is not None:
        stmt = stmt.where(AuditEvent.cohort_id == cohort_id)
    return list(
        db.scalars(stmt.order_by(AuditEvent.created_at.desc()).limit(limit).offset(offset)).all()
    )


def count_events(db: DbSession, *, cohort_id: uuid.UUID | None = None) -> int:
    from sqlalchemy import func

    stmt = select(func.count()).select_from(AuditEvent)
    if cohort_id is not None:
        stmt = stmt.where(AuditEvent.cohort_id == cohort_id)
    return int(db.scalar(stmt) or 0)
