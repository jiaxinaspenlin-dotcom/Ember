"""The Decision Log.

Decisions are never deleted.  A decision leaves ``active`` only by being
superseded (with a link to its replacement) or reversed (with a reason), and
both moves are recorded.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.auth import permissions
from app.core.enums import AuditAction, DecisionStatus, NotificationType
from app.core.errors import (
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.db.base import utcnow
from app.models.action import Decision
from app.models.cohort import Cohort, CohortMembership
from app.models.message import Message
from app.services import audit, notifications

Actor = CohortMembership

ALLOWED_TRANSITIONS: dict[DecisionStatus, frozenset[DecisionStatus]] = {
    DecisionStatus.ACTIVE: frozenset({DecisionStatus.SUPERSEDED, DecisionStatus.REVERSED}),
    DecisionStatus.SUPERSEDED: frozenset(),
    DecisionStatus.REVERSED: frozenset(),
}


@dataclass(slots=True)
class DecisionFilters:
    query: str | None = None
    status: DecisionStatus | None = None
    channel_id: uuid.UUID | None = None
    author_id: uuid.UUID | None = None
    related_project: str | None = None


@dataclass(slots=True)
class DecisionView:
    decision: Decision
    can_supersede: bool
    can_reverse: bool
    can_edit: bool


def can_transition(current: DecisionStatus, target: DecisionStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def get_decision(db: DbSession, cohort: Cohort, decision_id: uuid.UUID) -> Decision:
    decision = db.get(Decision, decision_id)
    if decision is None or decision.cohort_id != cohort.id:
        raise NotFoundError("Decision not found.", code="DECISION_NOT_FOUND")
    return decision


def create_decision(
    db: DbSession,
    *,
    author: Actor,
    title: str,
    decision_text: str,
    context: str | None = None,
    related_project: str | None = None,
    source_message: Message | None = None,
) -> Decision:
    clean_title = " ".join(title.split())
    if len(clean_title) < 4:
        raise ValidationError(
            "Give the decision a title of at least 4 characters.", details={"field": "title"}
        )
    clean_text = decision_text.strip()
    if not clean_text:
        raise ValidationError(
            "Record what was decided.", details={"field": "decision_text"}
        )

    source_channel_id = None
    if source_message is not None:
        permissions.require_convertible_message(source_message)
        source_channel_id = source_message.channel_id

    decision = Decision(
        cohort_id=author.cohort_id,
        title=clean_title[:160],
        decision_text=clean_text,
        context=(context or "").strip() or None,
        original_message_id=source_message.id if source_message else None,
        source_channel_id=source_channel_id,
        author_id=author.user_id,
        related_project=(related_project or "").strip()[:160] or None,
        status=DecisionStatus.ACTIVE,
    )
    db.add(decision)
    db.flush()
    audit.record(
        db,
        AuditAction.DECISION_CREATED,
        actor_id=author.user_id,
        cohort_id=author.cohort_id,
        entity_type="decision",
        entity_id=decision.id,
    )
    db.flush()
    return decision


def update_decision(
    db: DbSession,
    *,
    actor: Actor,
    decision: Decision,
    title: str | None = None,
    decision_text: str | None = None,
    context: str | None = None,
    related_project: str | None = None,
) -> Decision:
    if not permissions.can_edit_decision(decision, actor):
        raise PermissionDeniedError("You cannot edit this decision.")
    if decision.status is not DecisionStatus.ACTIVE:
        raise InvalidStateTransitionError(
            "Only active decisions can be edited.", code="DECISION_NOT_ACTIVE"
        )
    if title is not None:
        clean = " ".join(title.split())
        if len(clean) < 4:
            raise ValidationError(
                "Give the decision a title of at least 4 characters.",
                details={"field": "title"},
            )
        decision.title = clean[:160]
    if decision_text is not None:
        cleaned = decision_text.strip()
        if not cleaned:
            raise ValidationError(
                "Record what was decided.", details={"field": "decision_text"}
            )
        decision.decision_text = cleaned
    if context is not None:
        decision.context = context.strip() or None
    if related_project is not None:
        decision.related_project = related_project.strip()[:160] or None
    db.flush()
    return decision


def supersede_decision(
    db: DbSession,
    *,
    actor: Actor,
    decision: Decision,
    replacement_id: uuid.UUID | None = None,
    replacement: Decision | None = None,
) -> Decision:
    """Mark a decision superseded and link the decision that replaces it."""

    if not can_transition(decision.status, DecisionStatus.SUPERSEDED):
        raise InvalidStateTransitionError(
            f"A {decision.status.label.lower()} decision cannot be superseded.",
            code="DECISION_INVALID_TRANSITION",
        )
    if not permissions.can_supersede_decision(decision, actor):
        raise PermissionDeniedError(
            "Only the author or an administrator can supersede this decision."
        )

    if replacement is None:
        if replacement_id is None:
            raise ValidationError(
                "Choose the decision that replaces this one.",
                details={"field": "superseded_by_id"},
            )
        replacement = get_decision(db, actor.cohort, replacement_id)
    if replacement.id == decision.id:
        raise ValidationError(
            "A decision cannot supersede itself.", code="DECISION_SELF_SUPERSEDE"
        )
    if replacement.status is not DecisionStatus.ACTIVE:
        raise ValidationError(
            "The replacement decision must itself be active.",
            code="DECISION_REPLACEMENT_NOT_ACTIVE",
        )

    decision.status = DecisionStatus.SUPERSEDED
    decision.superseded_by_id = replacement.id
    decision.superseded_at = utcnow()
    audit.record(
        db,
        AuditAction.DECISION_SUPERSEDED,
        actor_id=actor.user_id,
        cohort_id=decision.cohort_id,
        entity_type="decision",
        entity_id=decision.id,
        context={"superseded_by": str(replacement.id)},
    )
    if decision.author_id != actor.user_id:
        notifications.create_notification(
            db,
            cohort_id=decision.cohort_id,
            recipient_id=decision.author_id,
            notification_type=NotificationType.DECISION_CHANGED,
            title=f"{actor.user.display_name} superseded your decision",
            body=decision.title,
            link_path=f"/decisions/{decision.id}",
            actor_id=actor.user_id,
            decision_id=decision.id,
        )
    db.flush()
    return decision


def reverse_decision(
    db: DbSession, *, actor: Actor, decision: Decision, reason: str | None = None
) -> Decision:
    if not can_transition(decision.status, DecisionStatus.REVERSED):
        raise InvalidStateTransitionError(
            f"A {decision.status.label.lower()} decision cannot be reversed.",
            code="DECISION_INVALID_TRANSITION",
        )
    if not permissions.can_reverse_decision(decision, actor):
        raise PermissionDeniedError(
            "Only the author or an administrator can reverse this decision."
        )
    decision.status = DecisionStatus.REVERSED
    decision.reversed_at = utcnow()
    decision.reversed_by_id = actor.user_id
    decision.reversal_reason = (reason or "").strip() or None
    audit.record(
        db,
        AuditAction.DECISION_REVERSED,
        actor_id=actor.user_id,
        cohort_id=decision.cohort_id,
        entity_type="decision",
        entity_id=decision.id,
    )
    if decision.author_id != actor.user_id:
        notifications.create_notification(
            db,
            cohort_id=decision.cohort_id,
            recipient_id=decision.author_id,
            notification_type=NotificationType.DECISION_CHANGED,
            title=f"{actor.user.display_name} reversed your decision",
            body=decision.title,
            link_path=f"/decisions/{decision.id}",
            actor_id=actor.user_id,
            decision_id=decision.id,
        )
    db.flush()
    return decision


def _filtered_query(filters: DecisionFilters, *, cohort_id: uuid.UUID) -> Select[tuple[Decision]]:
    stmt = select(Decision).where(Decision.cohort_id == cohort_id)
    if filters.status is not None:
        stmt = stmt.where(Decision.status == filters.status)
    if filters.channel_id is not None:
        stmt = stmt.where(Decision.source_channel_id == filters.channel_id)
    if filters.author_id is not None:
        stmt = stmt.where(Decision.author_id == filters.author_id)
    if filters.related_project:
        stmt = stmt.where(
            func.lower(func.coalesce(Decision.related_project, ""))
            == filters.related_project.strip().lower()
        )
    if filters.query:
        term = filters.query.strip()
        pattern = f"%{term.lower()}%"
        stmt = stmt.where(
            or_(
                Decision.search_vector.op("@@")(func.plainto_tsquery("english", term)),
                func.lower(Decision.title).like(pattern),
            )
        )
    return stmt


def list_decisions(
    db: DbSession, *, cohort: Cohort, filters: DecisionFilters, limit: int = 25, offset: int = 0
) -> tuple[list[Decision], int]:
    stmt = _filtered_query(filters, cohort_id=cohort.id)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    items = list(
        db.scalars(stmt.order_by(Decision.created_at.desc()).limit(limit).offset(offset)).all()
    )
    return items, total


def list_active_for_selection(
    db: DbSession, *, cohort: Cohort, exclude_id: uuid.UUID | None = None, limit: int = 50
) -> list[Decision]:
    """Active decisions offered as replacements when superseding."""

    stmt = select(Decision).where(
        Decision.cohort_id == cohort.id, Decision.status == DecisionStatus.ACTIVE
    )
    if exclude_id is not None:
        stmt = stmt.where(Decision.id != exclude_id)
    return list(db.scalars(stmt.order_by(Decision.created_at.desc()).limit(limit)).all())


def list_projects(db: DbSession, *, cohort: Cohort, limit: int = 100) -> list[str]:
    return [
        value
        for value in db.scalars(
            select(Decision.related_project)
            .where(
                Decision.cohort_id == cohort.id, Decision.related_project.is_not(None)
            )
            .distinct()
            .order_by(Decision.related_project.asc())
            .limit(limit)
        ).all()
        if value
    ]


def build_view(decision: Decision, *, viewer: Actor) -> DecisionView:
    return DecisionView(
        decision=decision,
        can_supersede=permissions.can_supersede_decision(decision, viewer),
        can_reverse=permissions.can_reverse_decision(decision, viewer),
        can_edit=permissions.can_edit_decision(decision, viewer)
        and decision.status is DecisionStatus.ACTIVE,
    )


def build_views(items: list[Decision], *, viewer: Actor) -> list[DecisionView]:
    return [build_view(item, viewer=viewer) for item in items]


def count_by_status(db: DbSession, *, cohort_id: uuid.UUID, status: DecisionStatus) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Decision)
            .where(Decision.cohort_id == cohort_id, Decision.status == status)
        )
        or 0
    )
