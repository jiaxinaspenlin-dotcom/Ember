"""Help requests and the cohort Help Queue.

The state machine lives here, in Python:

    open ──claim──> claimed ──resolve──> resolved ──reopen──> open
     │                 │                                        ▲
     │                 └──unclaim──> open                       │
     └──cancel──> cancelled ─────────reopen──────────────────---┘
     └──resolve──> resolved
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.auth import permissions
from app.core.enums import (
    AuditAction,
    HelpCategory,
    HelpRequestStatus,
    NotificationType,
    Priority,
)
from app.core.errors import (
    InvalidStateTransitionError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.db.base import utcnow
from app.models.action import HelpRequest
from app.models.cohort import Cohort, CohortMembership
from app.models.message import Message
from app.models.user import User
from app.services import audit, notifications

Actor = CohortMembership

# Which transitions the state machine permits, independent of who is asking.
ALLOWED_TRANSITIONS: dict[HelpRequestStatus, frozenset[HelpRequestStatus]] = {
    HelpRequestStatus.OPEN: frozenset(
        {HelpRequestStatus.CLAIMED, HelpRequestStatus.RESOLVED, HelpRequestStatus.CANCELLED}
    ),
    HelpRequestStatus.CLAIMED: frozenset(
        {HelpRequestStatus.OPEN, HelpRequestStatus.RESOLVED, HelpRequestStatus.CANCELLED}
    ),
    HelpRequestStatus.RESOLVED: frozenset({HelpRequestStatus.OPEN}),
    HelpRequestStatus.CANCELLED: frozenset({HelpRequestStatus.OPEN}),
}


@dataclass(slots=True)
class HelpFilters:
    status: HelpRequestStatus | None = None
    category: HelpCategory | None = None
    urgency: Priority | None = None
    assigned_to_me: bool = False
    created_by_me: bool = False
    unclaimed: bool = False
    query: str | None = None


@dataclass(slots=True)
class HelpRequestView:
    help_request: HelpRequest
    can_claim: bool
    can_unclaim: bool
    can_resolve: bool
    can_cancel: bool
    can_reopen: bool
    can_edit: bool


def can_transition(
    current: HelpRequestStatus, target: HelpRequestStatus
) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def _require_transition(
    help_request: HelpRequest, target: HelpRequestStatus
) -> None:
    if not can_transition(help_request.status, target):
        raise InvalidStateTransitionError(
            f"A {help_request.status.label.lower()} help request cannot become "
            f"{target.label.lower()}.",
            code="HELP_REQUEST_INVALID_TRANSITION",
        )


def get_help_request(
    db: DbSession, cohort: Cohort, help_request_id: uuid.UUID
) -> HelpRequest:
    help_request = db.get(HelpRequest, help_request_id)
    if help_request is None or help_request.cohort_id != cohort.id:
        raise NotFoundError("Help request not found.", code="HELP_REQUEST_NOT_FOUND")
    return help_request


# ---------------------------------------------------------------------------
# Creation
# ---------------------------------------------------------------------------


def create_help_request(
    db: DbSession,
    *,
    requester: Actor,
    title: str,
    description: str,
    category: HelpCategory = HelpCategory.OTHER,
    urgency: Priority = Priority.NORMAL,
    source_message: Message | None = None,
) -> HelpRequest:
    """Create a help request, optionally converted from a public channel message."""

    clean_title = " ".join(title.split())
    if len(clean_title) < 4:
        raise ValidationError(
            "Give the request a title of at least 4 characters.", details={"field": "title"}
        )
    clean_description = description.strip()
    if not clean_description:
        raise ValidationError(
            "Describe what you need help with.", details={"field": "description"}
        )

    source_channel_id = None
    if source_message is not None:
        permissions.require_convertible_message(source_message)
        source_channel_id = source_message.channel_id

    help_request = HelpRequest(
        cohort_id=requester.cohort_id,
        title=clean_title[:160],
        description=clean_description,
        original_message_id=source_message.id if source_message else None,
        requester_id=requester.user_id,
        source_channel_id=source_channel_id,
        category=category,
        urgency=urgency,
        status=HelpRequestStatus.OPEN,
    )
    db.add(help_request)
    db.flush()
    audit.record(
        db,
        AuditAction.HELP_REQUEST_CREATED,
        actor_id=requester.user_id,
        cohort_id=requester.cohort_id,
        entity_type="help_request",
        entity_id=help_request.id,
        context={"category": category.value, "urgency": urgency.value},
    )
    db.flush()
    return help_request


# ---------------------------------------------------------------------------
# Transitions
# ---------------------------------------------------------------------------


def claim_help_request(db: DbSession, *, actor: Actor, help_request: HelpRequest) -> HelpRequest:
    _require_transition(help_request, HelpRequestStatus.CLAIMED)
    if not permissions.can_claim_help_request(help_request, actor):
        raise PermissionDeniedError(
            "You cannot claim your own help request.", code="CANNOT_CLAIM_OWN_REQUEST"
        )
    help_request.status = HelpRequestStatus.CLAIMED
    help_request.assigned_helper_id = actor.user_id
    help_request.claimed_at = utcnow()
    notifications.create_notification(
        db,
        cohort_id=help_request.cohort_id,
        recipient_id=help_request.requester_id,
        notification_type=NotificationType.HELP_REQUEST_CLAIMED,
        title=f"{actor.user.display_name} claimed your help request",
        body=help_request.title,
        link_path=f"/help/{help_request.id}",
        actor_id=actor.user_id,
        help_request_id=help_request.id,
    )
    audit.record(
        db,
        AuditAction.HELP_REQUEST_CLAIMED,
        actor_id=actor.user_id,
        cohort_id=help_request.cohort_id,
        entity_type="help_request",
        entity_id=help_request.id,
    )
    db.flush()
    return help_request


def unclaim_help_request(
    db: DbSession, *, actor: Actor, help_request: HelpRequest
) -> HelpRequest:
    _require_transition(help_request, HelpRequestStatus.OPEN)
    if not permissions.can_unclaim_help_request(help_request, actor):
        raise PermissionDeniedError(
            "Only the assigned helper or an administrator can release this request."
        )
    previous_helper = help_request.assigned_helper_id
    help_request.status = HelpRequestStatus.OPEN
    help_request.assigned_helper_id = None
    help_request.claimed_at = None
    if previous_helper is not None and previous_helper != actor.user_id:
        notifications.create_notification(
            db,
            cohort_id=help_request.cohort_id,
            recipient_id=previous_helper,
            notification_type=NotificationType.HELP_REQUEST_ASSIGNED,
            title="A help request you claimed was released",
            body=help_request.title,
            link_path=f"/help/{help_request.id}",
            actor_id=actor.user_id,
            help_request_id=help_request.id,
        )
    audit.record(
        db,
        AuditAction.HELP_REQUEST_UNCLAIMED,
        actor_id=actor.user_id,
        cohort_id=help_request.cohort_id,
        entity_type="help_request",
        entity_id=help_request.id,
    )
    db.flush()
    return help_request


def resolve_help_request(
    db: DbSession,
    *,
    actor: Actor,
    help_request: HelpRequest,
    resolution_note: str | None = None,
) -> HelpRequest:
    _require_transition(help_request, HelpRequestStatus.RESOLVED)
    if not permissions.can_resolve_help_request(help_request, actor):
        raise PermissionDeniedError(
            "Only the requester, the assigned helper, or an administrator can resolve this."
        )
    help_request.status = HelpRequestStatus.RESOLVED
    help_request.resolved_at = utcnow()
    if resolution_note is not None:
        help_request.resolution_note = resolution_note.strip() or None

    recipients = {help_request.requester_id}
    if help_request.assigned_helper_id:
        recipients.add(help_request.assigned_helper_id)
    recipients.discard(actor.user_id)
    notifications.create_many(
        db,
        cohort_id=help_request.cohort_id,
        recipient_ids=recipients,
        notification_type=NotificationType.HELP_REQUEST_RESOLVED,
        title=f"{actor.user.display_name} resolved a help request",
        body=help_request.title,
        link_path=f"/help/{help_request.id}",
        actor_id=actor.user_id,
        help_request_id=help_request.id,
    )
    audit.record(
        db,
        AuditAction.HELP_REQUEST_RESOLVED,
        actor_id=actor.user_id,
        cohort_id=help_request.cohort_id,
        entity_type="help_request",
        entity_id=help_request.id,
    )
    db.flush()
    return help_request


def cancel_help_request(db: DbSession, *, actor: Actor, help_request: HelpRequest) -> HelpRequest:
    _require_transition(help_request, HelpRequestStatus.CANCELLED)
    if not permissions.can_cancel_help_request(help_request, actor):
        raise PermissionDeniedError(
            "Only the requester or an administrator can cancel this request."
        )
    previous_helper = help_request.assigned_helper_id
    help_request.status = HelpRequestStatus.CANCELLED
    help_request.cancelled_at = utcnow()
    help_request.assigned_helper_id = None
    help_request.claimed_at = None
    if previous_helper and previous_helper != actor.user_id:
        notifications.create_notification(
            db,
            cohort_id=help_request.cohort_id,
            recipient_id=previous_helper,
            notification_type=NotificationType.HELP_REQUEST_ASSIGNED,
            title="A help request you claimed was cancelled",
            body=help_request.title,
            link_path=f"/help/{help_request.id}",
            actor_id=actor.user_id,
            help_request_id=help_request.id,
        )
    audit.record(
        db,
        AuditAction.HELP_REQUEST_CANCELLED,
        actor_id=actor.user_id,
        cohort_id=help_request.cohort_id,
        entity_type="help_request",
        entity_id=help_request.id,
    )
    db.flush()
    return help_request


def reopen_help_request(db: DbSession, *, actor: Actor, help_request: HelpRequest) -> HelpRequest:
    _require_transition(help_request, HelpRequestStatus.OPEN)
    if not permissions.can_reopen_help_request(help_request, actor):
        raise PermissionDeniedError(
            "Only the requester or an administrator can reopen this request."
        )
    help_request.status = HelpRequestStatus.OPEN
    help_request.resolved_at = None
    help_request.cancelled_at = None
    help_request.assigned_helper_id = None
    help_request.claimed_at = None
    audit.record(
        db,
        AuditAction.HELP_REQUEST_REOPENED,
        actor_id=actor.user_id,
        cohort_id=help_request.cohort_id,
        entity_type="help_request",
        entity_id=help_request.id,
    )
    notifications.create_notification(
        db,
        cohort_id=help_request.cohort_id,
        recipient_id=help_request.requester_id,
        notification_type=NotificationType.HELP_REQUEST_REOPENED,
        title=f"{actor.user.display_name} reopened a help request",
        body=help_request.title,
        link_path=f"/help/{help_request.id}",
        actor_id=actor.user_id,
        help_request_id=help_request.id,
    )
    db.flush()
    return help_request


def update_help_request(
    db: DbSession,
    *,
    actor: Actor,
    help_request: HelpRequest,
    title: str | None = None,
    description: str | None = None,
    category: HelpCategory | None = None,
    urgency: Priority | None = None,
) -> HelpRequest:
    if not permissions.can_edit_help_request(help_request, actor):
        raise PermissionDeniedError("You cannot edit this help request.")
    if title is not None:
        clean = " ".join(title.split())
        if len(clean) < 4:
            raise ValidationError(
                "Give the request a title of at least 4 characters.", details={"field": "title"}
            )
        help_request.title = clean[:160]
    if description is not None:
        cleaned = description.strip()
        if not cleaned:
            raise ValidationError(
                "Describe what you need help with.", details={"field": "description"}
            )
        help_request.description = cleaned
    if category is not None:
        help_request.category = category
    if urgency is not None:
        help_request.urgency = urgency
    db.flush()
    return help_request


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def _filtered_query(
    filters: HelpFilters, *, cohort_id: uuid.UUID, user_id: uuid.UUID
) -> Select[tuple[HelpRequest]]:
    stmt = select(HelpRequest).where(HelpRequest.cohort_id == cohort_id)
    if filters.status is not None:
        stmt = stmt.where(HelpRequest.status == filters.status)
    if filters.category is not None:
        stmt = stmt.where(HelpRequest.category == filters.category)
    if filters.urgency is not None:
        stmt = stmt.where(HelpRequest.urgency == filters.urgency)
    if filters.assigned_to_me:
        stmt = stmt.where(HelpRequest.assigned_helper_id == user_id)
    if filters.created_by_me:
        stmt = stmt.where(HelpRequest.requester_id == user_id)
    if filters.unclaimed:
        stmt = stmt.where(
            HelpRequest.status == HelpRequestStatus.OPEN,
            HelpRequest.assigned_helper_id.is_(None),
        )
    if filters.query:
        pattern = f"%{filters.query.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(HelpRequest.title).like(pattern),
                func.lower(HelpRequest.description).like(pattern),
            )
        )
    return stmt


def list_help_requests(
    db: DbSession,
    *,
    cohort: Cohort,
    user: User,
    filters: HelpFilters,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[HelpRequest], int]:
    stmt = _filtered_query(filters, cohort_id=cohort.id, user_id=user.id)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    items = list(
        db.scalars(
            stmt.order_by(HelpRequest.created_at.desc()).limit(limit).offset(offset)
        ).all()
    )
    return items, total


def build_view(help_request: HelpRequest, *, viewer: Actor) -> HelpRequestView:
    return HelpRequestView(
        help_request=help_request,
        can_claim=permissions.can_claim_help_request(help_request, viewer),
        can_unclaim=permissions.can_unclaim_help_request(help_request, viewer),
        can_resolve=permissions.can_resolve_help_request(help_request, viewer),
        can_cancel=permissions.can_cancel_help_request(help_request, viewer),
        can_reopen=permissions.can_reopen_help_request(help_request, viewer),
        can_edit=permissions.can_edit_help_request(help_request, viewer),
    )


def build_views(items: list[HelpRequest], *, viewer: Actor) -> list[HelpRequestView]:
    return [build_view(item, viewer=viewer) for item in items]


def count_by_status(db: DbSession, *, cohort_id: uuid.UUID, status: HelpRequestStatus) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(HelpRequest)
            .where(HelpRequest.cohort_id == cohort_id, HelpRequest.status == status)
        )
        or 0
    )


def count_assigned_to(db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(HelpRequest)
            .where(
                HelpRequest.cohort_id == cohort_id,
                HelpRequest.assigned_helper_id == user_id,
                HelpRequest.status == HelpRequestStatus.CLAIMED,
            )
        )
        or 0
    )
