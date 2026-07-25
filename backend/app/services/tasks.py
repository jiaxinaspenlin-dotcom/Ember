"""Tasks: creation, assignment and status transitions.

Permission model (all enforced here, never in the browser):

* creator and administrators -- full management (edit, reassign, delete-equivalent)
* assignee -- may change status only
* everyone else -- read only
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session as DbSession

from app.auth import permissions
from app.core.enums import AuditAction, NotificationType, Priority, TaskStatus
from app.core.errors import NotFoundError, ValidationError
from app.db.base import utcnow
from app.models.action import Task
from app.models.cohort import Cohort, CohortMembership
from app.models.message import Message
from app.models.user import User
from app.services import accounts, audit, cohorts, forth, notifications

Actor = CohortMembership

# Every status is reachable from every other status: work really does move
# backwards sometimes. Only the completion timestamp is bookkeeping.
ALLOWED_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.TODO: frozenset({TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED, TaskStatus.DONE}),
    TaskStatus.IN_PROGRESS: frozenset({TaskStatus.TODO, TaskStatus.BLOCKED, TaskStatus.DONE}),
    TaskStatus.BLOCKED: frozenset({TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.DONE}),
    TaskStatus.DONE: frozenset({TaskStatus.TODO, TaskStatus.IN_PROGRESS, TaskStatus.BLOCKED}),
}


@dataclass(slots=True)
class TaskFilters:
    status: TaskStatus | None = None
    priority: Priority | None = None
    assignee_id: uuid.UUID | None = None
    creator_id: uuid.UUID | None = None
    assigned_to_me: bool = False
    created_by_me: bool = False
    unassigned: bool = False
    query: str | None = None


@dataclass(slots=True)
class TaskView:
    task: Task
    can_manage: bool
    can_update_status: bool


def can_transition(current: TaskStatus, target: TaskStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def _require_cohort_member(
    db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID
) -> User:
    user = accounts.require_user(db, user_id)
    if cohorts.get_membership(db, cohort_id=cohort_id, user_id=user_id) is None:
        raise NotFoundError("Member not found.", code="USER_NOT_FOUND")
    return user


def get_task(db: DbSession, cohort: Cohort, task_id: uuid.UUID) -> Task:
    task = db.get(Task, task_id)
    if task is None or task.cohort_id != cohort.id:
        raise NotFoundError("Task not found.", code="TASK_NOT_FOUND")
    return task


def create_task(
    db: DbSession,
    *,
    creator: Actor,
    title: str,
    description: str | None = None,
    assignee_id: uuid.UUID | None = None,
    priority: Priority = Priority.NORMAL,
    due_at: dt.datetime | None = None,
    source_message: Message | None = None,
    forth_url: str | None = None,
) -> Task:
    clean_title = " ".join(title.split())
    if len(clean_title) < 3:
        raise ValidationError(
            "Give the task a title of at least 3 characters.", details={"field": "title"}
        )

    assignee: User | None = None
    if assignee_id is not None:
        assignee = _require_cohort_member(db, cohort_id=creator.cohort_id, user_id=assignee_id)

    source_channel_id = None
    if source_message is not None:
        permissions.require_convertible_message(source_message)
        source_channel_id = source_message.channel_id

    task = Task(
        cohort_id=creator.cohort_id,
        title=clean_title[:160],
        description=(description or "").strip() or None,
        creator_id=creator.user_id,
        assignee_id=assignee.id if assignee else None,
        source_message_id=source_message.id if source_message else None,
        source_channel_id=source_channel_id,
        status=TaskStatus.TODO,
        priority=priority,
        due_at=due_at,
        forth_url=forth.normalize_forth_url(forth_url),
    )
    db.add(task)
    db.flush()

    if assignee is not None:
        _notify_assignment(db, task=task, actor=creator, assignee_id=assignee.id)
    audit.record(
        db,
        AuditAction.TASK_CREATED,
        actor_id=creator.user_id,
        cohort_id=creator.cohort_id,
        entity_type="task",
        entity_id=task.id,
        context={"priority": priority.value, "assigned": assignee is not None},
    )
    db.flush()
    return task


def _notify_assignment(
    db: DbSession, *, task: Task, actor: Actor, assignee_id: uuid.UUID
) -> None:
    notifications.create_notification(
        db,
        cohort_id=task.cohort_id,
        recipient_id=assignee_id,
        notification_type=NotificationType.TASK_ASSIGNED,
        title=f"{actor.user.display_name} assigned you a task",
        body=task.title,
        link_path=f"/tasks/{task.id}",
        actor_id=actor.user_id,
        task_id=task.id,
    )


def assign_task(
    db: DbSession, *, actor: Actor, task: Task, assignee_id: uuid.UUID | None
) -> Task:
    """Assign or unassign. Only the creator or an administrator may do this."""

    permissions.require_task_management(task, actor)
    previous = task.assignee_id
    if assignee_id is None:
        task.assignee_id = None
    else:
        assignee = _require_cohort_member(db, cohort_id=actor.cohort_id, user_id=assignee_id)
        task.assignee_id = assignee.id
        if previous != assignee.id:
            _notify_assignment(db, task=task, actor=actor, assignee_id=assignee.id)
    audit.record(
        db,
        AuditAction.TASK_ASSIGNED,
        actor_id=actor.user_id,
        cohort_id=task.cohort_id,
        entity_type="task",
        entity_id=task.id,
        context={"assignee": str(assignee_id) if assignee_id else None},
    )
    db.flush()
    return task


def update_task_status(db: DbSession, *, actor: Actor, task: Task, status: TaskStatus) -> Task:
    permissions.require_task_status_update(task, actor)
    if task.status is status:
        return task
    if not can_transition(task.status, status):  # pragma: no cover - all moves allowed today
        from app.core.errors import InvalidStateTransitionError

        raise InvalidStateTransitionError(
            f"A task cannot move from {task.status.label} to {status.label}.",
            code="TASK_INVALID_TRANSITION",
        )

    previous = task.status
    task.status = status
    task.completed_at = utcnow() if status is TaskStatus.DONE else None

    recipients = {task.creator_id}
    if task.assignee_id:
        recipients.add(task.assignee_id)
    recipients.discard(actor.user_id)
    notifications.create_many(
        db,
        cohort_id=task.cohort_id,
        recipient_ids=recipients,
        notification_type=NotificationType.TASK_STATUS_CHANGED,
        title=f"{actor.user.display_name} moved a task to {status.label}",
        body=task.title,
        link_path=f"/tasks/{task.id}",
        actor_id=actor.user_id,
        task_id=task.id,
    )
    audit.record(
        db,
        AuditAction.TASK_STATUS_CHANGED,
        actor_id=actor.user_id,
        cohort_id=task.cohort_id,
        entity_type="task",
        entity_id=task.id,
        context={"from": previous.value, "to": status.value},
    )
    db.flush()
    return task


def update_task(
    db: DbSession,
    *,
    actor: Actor,
    task: Task,
    title: str | None = None,
    description: str | None = None,
    priority: Priority | None = None,
    due_at: dt.datetime | None = None,
    clear_due_at: bool = False,
    forth_url: str | None = None,
    clear_forth_url: bool = False,
) -> Task:
    permissions.require_task_management(task, actor)
    if title is not None:
        clean = " ".join(title.split())
        if len(clean) < 3:
            raise ValidationError(
                "Give the task a title of at least 3 characters.", details={"field": "title"}
            )
        task.title = clean[:160]
    if description is not None:
        task.description = description.strip() or None
    if priority is not None:
        task.priority = priority
    if clear_due_at:
        task.due_at = None
    elif due_at is not None:
        task.due_at = due_at
    if clear_forth_url:
        task.forth_url = None
    elif forth_url is not None:
        task.forth_url = forth.normalize_forth_url(forth_url)
    db.flush()
    return task


def _filtered_query(
    filters: TaskFilters, *, cohort_id: uuid.UUID, user_id: uuid.UUID
) -> Select[tuple[Task]]:
    stmt = select(Task).where(Task.cohort_id == cohort_id)
    if filters.status is not None:
        stmt = stmt.where(Task.status == filters.status)
    if filters.priority is not None:
        stmt = stmt.where(Task.priority == filters.priority)
    if filters.assignee_id is not None:
        stmt = stmt.where(Task.assignee_id == filters.assignee_id)
    if filters.creator_id is not None:
        stmt = stmt.where(Task.creator_id == filters.creator_id)
    if filters.assigned_to_me:
        stmt = stmt.where(Task.assignee_id == user_id)
    if filters.created_by_me:
        stmt = stmt.where(Task.creator_id == user_id)
    if filters.unassigned:
        stmt = stmt.where(Task.assignee_id.is_(None))
    if filters.query:
        pattern = f"%{filters.query.strip().lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Task.title).like(pattern),
                func.lower(func.coalesce(Task.description, "")).like(pattern),
            )
        )
    return stmt


def list_tasks(
    db: DbSession,
    *,
    cohort: Cohort,
    user: User,
    filters: TaskFilters,
    limit: int = 25,
    offset: int = 0,
) -> tuple[list[Task], int]:
    stmt = _filtered_query(filters, cohort_id=cohort.id, user_id=user.id)
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    items = list(
        db.scalars(
            stmt.order_by(Task.status.asc(), Task.created_at.desc()).limit(limit).offset(offset)
        ).all()
    )
    return items, total


def open_tasks_for_user(
    db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID, limit: int = 5
) -> list[Task]:
    return list(
        db.scalars(
            select(Task)
            .where(
                Task.cohort_id == cohort_id,
                Task.assignee_id == user_id,
                Task.status != TaskStatus.DONE,
            )
            .order_by(Task.due_at.asc().nulls_last(), Task.created_at.desc())
            .limit(limit)
        ).all()
    )


def count_open_for_user(db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Task)
            .where(
                Task.cohort_id == cohort_id,
                Task.assignee_id == user_id,
                Task.status != TaskStatus.DONE,
            )
        )
        or 0
    )


def build_view(task: Task, *, viewer: Actor) -> TaskView:
    return TaskView(
        task=task,
        can_manage=permissions.can_manage_task(task, viewer),
        can_update_status=permissions.can_update_task_status(task, viewer),
    )


def build_views(items: list[Task], *, viewer: Actor) -> list[TaskView]:
    return [build_view(item, viewer=viewer) for item in items]
