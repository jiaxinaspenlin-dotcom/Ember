"""Task routes."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Query, status

from app.api.dependencies import CohortDep, DbDep, PaginationDep
from app.core.enums import Priority, TaskStatus
from app.schemas.common import Page
from app.schemas.content import (
    TaskAssignRequest,
    TaskCreateRequest,
    TaskOut,
    TaskStatusRequest,
    TaskUpdateRequest,
)
from app.schemas.serializers import task_out
from app.services import messages, tasks

router = APIRouter(prefix="/api/tasks", tags=["tasks"])


@router.get("", response_model=Page[TaskOut], summary="List tasks")
def list_tasks(
    db: DbDep,
    ctx: CohortDep,
    pagination: PaginationDep,
    status_filter: Annotated[TaskStatus | None, Query(alias="status")] = None,
    priority: Priority | None = None,
    assignee_id: uuid.UUID | None = None,
    creator_id: uuid.UUID | None = None,
    assigned_to_me: bool = False,
    created_by_me: bool = False,
    unassigned: bool = False,
    q: Annotated[str | None, Query(max_length=120)] = None,
) -> Page[TaskOut]:
    items, total = tasks.list_tasks(
        db,
        cohort=ctx.cohort,
        user=ctx.user,
        filters=tasks.TaskFilters(
            status=status_filter,
            priority=priority,
            assignee_id=assignee_id,
            creator_id=creator_id,
            assigned_to_me=assigned_to_me,
            created_by_me=created_by_me,
            unassigned=unassigned,
            query=q,
        ),
        limit=pagination.limit,
        offset=pagination.offset,
    )
    views = tasks.build_views(items, viewer=ctx.member)
    return Page[TaskOut](
        items=[task_out(view) for view in views],
        total=total,
        limit=pagination.limit,
        offset=pagination.offset,
        has_more=pagination.offset + len(items) < total,
    )


@router.post(
    "",
    response_model=TaskOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a task (optionally from a message)",
)
def create_task(payload: TaskCreateRequest, db: DbDep, ctx: CohortDep) -> TaskOut:
    source = (
        messages.get_visible_message(
            db, message_id=payload.source_message_id, actor=ctx.member
        )
        if payload.source_message_id
        else None
    )
    task = tasks.create_task(
        db,
        creator=ctx.member,
        title=payload.title,
        description=payload.description,
        assignee_id=payload.assignee_id,
        priority=payload.priority,
        due_at=payload.due_at,
        source_message=source,
    )
    db.commit()
    return task_out(tasks.build_view(task, viewer=ctx.member))


@router.get("/{task_id}", response_model=TaskOut, summary="Read a task")
def read_task(task_id: uuid.UUID, db: DbDep, ctx: CohortDep) -> TaskOut:
    task = tasks.get_task(db, ctx.cohort, task_id)
    return task_out(tasks.build_view(task, viewer=ctx.member))


@router.patch("/{task_id}", response_model=TaskOut, summary="Edit a task")
def update_task(
    task_id: uuid.UUID, payload: TaskUpdateRequest, db: DbDep, ctx: CohortDep
) -> TaskOut:
    task = tasks.update_task(
        db,
        actor=ctx.member,
        task=tasks.get_task(db, ctx.cohort, task_id),
        title=payload.title,
        description=payload.description,
        priority=payload.priority,
        due_at=payload.due_at,
        clear_due_at=payload.clear_due_at,
    )
    db.commit()
    return task_out(tasks.build_view(task, viewer=ctx.member))


@router.put("/{task_id}/assignee", response_model=TaskOut, summary="Assign or reassign a task")
def assign_task(
    task_id: uuid.UUID, payload: TaskAssignRequest, db: DbDep, ctx: CohortDep
) -> TaskOut:
    task = tasks.assign_task(
        db,
        actor=ctx.member,
        task=tasks.get_task(db, ctx.cohort, task_id),
        assignee_id=payload.assignee_id,
    )
    db.commit()
    return task_out(tasks.build_view(task, viewer=ctx.member))


@router.put("/{task_id}/status", response_model=TaskOut, summary="Update task status")
def update_status(
    task_id: uuid.UUID, payload: TaskStatusRequest, db: DbDep, ctx: CohortDep
) -> TaskOut:
    task = tasks.update_task_status(
        db, actor=ctx.member, task=tasks.get_task(db, ctx.cohort, task_id), status=payload.status
    )
    db.commit()
    return task_out(tasks.build_view(task, viewer=ctx.member))
