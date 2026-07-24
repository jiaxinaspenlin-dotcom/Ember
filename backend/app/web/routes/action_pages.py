"""Help Queue, Decision Log, Tasks, and the message-to-action menu."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.dependencies import DbDep
from app.core.enums import (
    DecisionStatus,
    HelpCategory,
    HelpRequestStatus,
    Priority,
    TaskStatus,
)
from app.core.errors import EmberError, ValidationError
from app.services import accounts, channels, decisions, help_requests, messages, profiles, tasks
from app.web.deps import PageCohort, page_context
from app.web.templating import render

router = APIRouter(tags=["web-actions"])

PAGE_SIZE = 20


def _enum_or_none(enum_cls: type, value: str | None):  # type: ignore[no-untyped-def]
    if not value:
        return None
    try:
        return enum_cls(value)
    except ValueError as exc:
        raise ValidationError(f"{value!r} is not a valid option.") from exc


def _parse_due(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(
            "Enter the due date as YYYY-MM-DD.", details={"field": "due_at"}
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


# ---------------------------------------------------------------------------
# Message -> action
# ---------------------------------------------------------------------------


@router.get(
    "/messages/{message_id}/actions",
    response_class=HTMLResponse,
    summary="Turn a message into an action",
)
def message_actions_page(
    request: Request, db: DbDep, ctx: PageCohort, message_id: uuid.UUID
) -> Response:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    view = messages.build_view(db, message, viewer=ctx.member)
    members, _ = profiles.list_directory(
        db, cohort=ctx.cohort, filters=profiles.DirectoryFilters(), limit=100
    )
    return render(
        request,
        "pages/message_actions.html",
        page_context(
            db,
            ctx,
            view=view,
            members=[membership.user for membership in members],
            active_nav="channels",
        ),
    )


# ---------------------------------------------------------------------------
# Help Queue
# ---------------------------------------------------------------------------


@router.get("/help", response_class=HTMLResponse, summary="Help Queue")
def help_queue(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    category: str | None = None,
    urgency: str | None = None,
    assigned_to_me: bool = False,
    created_by_me: bool = False,
    unclaimed: bool = False,
    q: Annotated[str | None, Query(max_length=120)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    filters = help_requests.HelpFilters(
        status=_enum_or_none(HelpRequestStatus, status_filter),
        category=_enum_or_none(HelpCategory, category),
        urgency=_enum_or_none(Priority, urgency),
        assigned_to_me=assigned_to_me,
        created_by_me=created_by_me,
        unclaimed=unclaimed,
        query=q,
    )
    items, total = help_requests.list_help_requests(
        db, cohort=ctx.cohort, user=ctx.user, filters=filters, limit=PAGE_SIZE, offset=offset
    )
    return render(
        request,
        "pages/help_queue.html",
        page_context(
            db,
            ctx,
            views=help_requests.build_views(items, viewer=ctx.member),
            total=total,
            offset=offset,
            page_size=PAGE_SIZE,
            filters=filters,
            query=q or "",
            counts={
                "open": help_requests.count_by_status(
                    db, cohort_id=ctx.cohort_id, status=HelpRequestStatus.OPEN
                ),
                "claimed": help_requests.count_by_status(
                    db, cohort_id=ctx.cohort_id, status=HelpRequestStatus.CLAIMED
                ),
                "resolved": help_requests.count_by_status(
                    db, cohort_id=ctx.cohort_id, status=HelpRequestStatus.RESOLVED
                ),
            },
            active_nav="help",
        ),
    )


@router.post("/help", summary="Create a help request")
def create_help_request(
    db: DbDep,
    ctx: PageCohort,
    title: Annotated[str, Form()],
    description: Annotated[str, Form()],
    category: Annotated[str, Form()] = HelpCategory.OTHER.value,
    urgency: Annotated[str, Form()] = Priority.NORMAL.value,
    source_message_id: Annotated[str | None, Form()] = None,
) -> Response:
    source = (
        messages.get_visible_message(
            db, message_id=uuid.UUID(source_message_id), actor=ctx.member
        )
        if source_message_id
        else None
    )
    help_request = help_requests.create_help_request(
        db,
        requester=ctx.member,
        title=title,
        description=description,
        category=HelpCategory(category),
        urgency=Priority(urgency),
        source_message=source,
    )
    db.commit()
    return RedirectResponse(
        f"/help/{help_request.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/help/{help_request_id}", response_class=HTMLResponse, summary="A help request")
def help_request_page(
    request: Request, db: DbDep, ctx: PageCohort, help_request_id: uuid.UUID
) -> Response:
    help_request = help_requests.get_help_request(db, ctx.cohort, help_request_id)
    source_message = (
        messages.get_message(db, help_request.original_message_id)
        if help_request.original_message_id
        else None
    )
    return render(
        request,
        "pages/help_request.html",
        page_context(
            db,
            ctx,
            view=help_requests.build_view(help_request, viewer=ctx.member),
            source_message=source_message,
            active_nav="help",
        ),
    )


@router.post("/help/{help_request_id}/{action}", summary="Help request transition")
def help_request_action(
    db: DbDep,
    ctx: PageCohort,
    help_request_id: uuid.UUID,
    action: str,
    resolution_note: Annotated[str | None, Form()] = None,
) -> Response:
    help_request = help_requests.get_help_request(db, ctx.cohort, help_request_id)
    if action == "claim":
        help_requests.claim_help_request(db, actor=ctx.member, help_request=help_request)
    elif action == "unclaim":
        help_requests.unclaim_help_request(db, actor=ctx.member, help_request=help_request)
    elif action == "resolve":
        help_requests.resolve_help_request(
            db,
            actor=ctx.member,
            help_request=help_request,
            resolution_note=resolution_note,
        )
    elif action == "cancel":
        help_requests.cancel_help_request(db, actor=ctx.member, help_request=help_request)
    elif action == "reopen":
        help_requests.reopen_help_request(db, actor=ctx.member, help_request=help_request)
    else:
        raise ValidationError("Unknown action.", code="UNKNOWN_ACTION")
    db.commit()
    return RedirectResponse(
        f"/help/{help_request_id}", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Decision Log
# ---------------------------------------------------------------------------


@router.get("/decisions", response_class=HTMLResponse, summary="Decision Log")
def decision_log(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    q: Annotated[str | None, Query(max_length=200)] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    channel_id: uuid.UUID | None = None,
    author_id: uuid.UUID | None = None,
    related_project: Annotated[str | None, Query(max_length=160)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    filters = decisions.DecisionFilters(
        query=q,
        status=_enum_or_none(DecisionStatus, status_filter),
        channel_id=channel_id,
        author_id=author_id,
        related_project=related_project,
    )
    items, total = decisions.list_decisions(
        db, cohort=ctx.cohort, filters=filters, limit=PAGE_SIZE, offset=offset
    )
    channel_items, _ = channels.list_channels(
        db, cohort=ctx.cohort, user=ctx.user, include_archived=True, limit=100
    )
    members, _ = profiles.list_directory(
        db, cohort=ctx.cohort, filters=profiles.DirectoryFilters(), limit=100
    )
    return render(
        request,
        "pages/decisions.html",
        page_context(
            db,
            ctx,
            views=decisions.build_views(items, viewer=ctx.member),
            total=total,
            offset=offset,
            page_size=PAGE_SIZE,
            filters=filters,
            query=q or "",
            all_channels=[item.channel for item in channel_items],
            all_members=[membership.user for membership in members],
            projects=decisions.list_projects(db, cohort=ctx.cohort),
            active_nav="decisions",
        ),
    )


@router.post("/decisions", summary="Record a decision")
def create_decision(
    db: DbDep,
    ctx: PageCohort,
    title: Annotated[str, Form()],
    decision_text: Annotated[str, Form()],
    context: Annotated[str, Form()] = "",
    related_project: Annotated[str, Form()] = "",
    source_message_id: Annotated[str | None, Form()] = None,
) -> Response:
    source = (
        messages.get_visible_message(
            db, message_id=uuid.UUID(source_message_id), actor=ctx.member
        )
        if source_message_id
        else None
    )
    decision = decisions.create_decision(
        db,
        author=ctx.member,
        title=title,
        decision_text=decision_text,
        context=context,
        related_project=related_project,
        source_message=source,
    )
    db.commit()
    return RedirectResponse(
        f"/decisions/{decision.id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/decisions/{decision_id}", response_class=HTMLResponse, summary="A decision")
def decision_page(
    request: Request, db: DbDep, ctx: PageCohort, decision_id: uuid.UUID
) -> Response:
    decision = decisions.get_decision(db, ctx.cohort, decision_id)
    source_message = (
        messages.get_message(db, decision.original_message_id)
        if decision.original_message_id
        else None
    )
    return render(
        request,
        "pages/decision.html",
        page_context(
            db,
            ctx,
            view=decisions.build_view(decision, viewer=ctx.member),
            source_message=source_message,
            replacement_options=decisions.list_active_for_selection(
                db, cohort=ctx.cohort, exclude_id=decision.id
            ),
            superseded_by=(
                decisions.get_decision(db, ctx.cohort, decision.superseded_by_id)
                if decision.superseded_by_id
                else None
            ),
            active_nav="decisions",
        ),
    )


@router.post("/decisions/{decision_id}/supersede", summary="Supersede a decision")
def supersede_decision(
    db: DbDep,
    ctx: PageCohort,
    decision_id: uuid.UUID,
    superseded_by_id: Annotated[uuid.UUID, Form()],
) -> Response:
    decisions.supersede_decision(
        db,
        actor=ctx.member,
        decision=decisions.get_decision(db, ctx.cohort, decision_id),
        replacement_id=superseded_by_id,
    )
    db.commit()
    return RedirectResponse(
        f"/decisions/{decision_id}", status_code=status.HTTP_303_SEE_OTHER
    )


@router.post("/decisions/{decision_id}/reverse", summary="Reverse a decision")
def reverse_decision(
    db: DbDep,
    ctx: PageCohort,
    decision_id: uuid.UUID,
    reason: Annotated[str, Form()] = "",
) -> Response:
    decisions.reverse_decision(
        db,
        actor=ctx.member,
        decision=decisions.get_decision(db, ctx.cohort, decision_id),
        reason=reason,
    )
    db.commit()
    return RedirectResponse(
        f"/decisions/{decision_id}", status_code=status.HTTP_303_SEE_OTHER
    )


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


@router.get("/tasks", response_class=HTMLResponse, summary="Tasks")
def tasks_page(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    priority: str | None = None,
    assignee_id: uuid.UUID | None = None,
    assigned_to_me: bool = False,
    created_by_me: bool = False,
    unassigned: bool = False,
    q: Annotated[str | None, Query(max_length=120)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    filters = tasks.TaskFilters(
        status=_enum_or_none(TaskStatus, status_filter),
        priority=_enum_or_none(Priority, priority),
        assignee_id=assignee_id,
        assigned_to_me=assigned_to_me,
        created_by_me=created_by_me,
        unassigned=unassigned,
        query=q,
    )
    items, total = tasks.list_tasks(
        db, cohort=ctx.cohort, user=ctx.user, filters=filters, limit=PAGE_SIZE, offset=offset
    )
    members, _ = profiles.list_directory(
        db, cohort=ctx.cohort, filters=profiles.DirectoryFilters(), limit=100
    )
    return render(
        request,
        "pages/tasks.html",
        page_context(
            db,
            ctx,
            views=tasks.build_views(items, viewer=ctx.member),
            total=total,
            offset=offset,
            page_size=PAGE_SIZE,
            filters=filters,
            query=q or "",
            all_members=[membership.user for membership in members],
            active_nav="tasks",
        ),
    )


@router.post("/tasks", summary="Create a task")
def create_task(
    db: DbDep,
    ctx: PageCohort,
    title: Annotated[str, Form()],
    description: Annotated[str, Form()] = "",
    assignee_id: Annotated[str | None, Form()] = None,
    priority: Annotated[str, Form()] = Priority.NORMAL.value,
    due_at: Annotated[str | None, Form()] = None,
    source_message_id: Annotated[str | None, Form()] = None,
) -> Response:
    source = (
        messages.get_visible_message(
            db, message_id=uuid.UUID(source_message_id), actor=ctx.member
        )
        if source_message_id
        else None
    )
    task = tasks.create_task(
        db,
        creator=ctx.member,
        title=title,
        description=description,
        assignee_id=uuid.UUID(assignee_id) if assignee_id else None,
        priority=Priority(priority),
        due_at=_parse_due(due_at),
        source_message=source,
    )
    db.commit()
    return RedirectResponse(f"/tasks/{task.id}", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/tasks/{task_id}", response_class=HTMLResponse, summary="A task")
def task_page(request: Request, db: DbDep, ctx: PageCohort, task_id: uuid.UUID) -> Response:
    task = tasks.get_task(db, ctx.cohort, task_id)
    members, _ = profiles.list_directory(
        db, cohort=ctx.cohort, filters=profiles.DirectoryFilters(), limit=100
    )
    source_message = (
        messages.get_message(db, task.source_message_id) if task.source_message_id else None
    )
    return render(
        request,
        "pages/task.html",
        page_context(
            db,
            ctx,
            view=tasks.build_view(task, viewer=ctx.member),
            all_members=[membership.user for membership in members],
            source_message=source_message,
            active_nav="tasks",
        ),
    )


@router.post("/tasks/{task_id}/status", summary="Update task status")
def update_task_status(
    db: DbDep,
    ctx: PageCohort,
    task_id: uuid.UUID,
    status_value: Annotated[str, Form(alias="status")],
) -> Response:
    tasks.update_task_status(
        db,
        actor=ctx.member,
        task=tasks.get_task(db, ctx.cohort, task_id),
        status=TaskStatus(status_value),
    )
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/tasks/{task_id}/assign", summary="Assign or reassign a task")
def assign_task(
    db: DbDep,
    ctx: PageCohort,
    task_id: uuid.UUID,
    assignee_id: Annotated[str, Form()] = "",
) -> Response:
    tasks.assign_task(
        db,
        actor=ctx.member,
        task=tasks.get_task(db, ctx.cohort, task_id),
        assignee_id=uuid.UUID(assignee_id) if assignee_id else None,
    )
    db.commit()
    return RedirectResponse(f"/tasks/{task_id}", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/hx/tasks/{task_id}/status",
    response_class=HTMLResponse,
    summary="Update task status inline (HTMX)",
)
def update_task_status_inline(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    task_id: uuid.UUID,
    status_value: Annotated[str, Form(alias="status")],
) -> Response:
    try:
        parsed = TaskStatus(status_value)
    except ValueError as exc:
        raise ValidationError("That status is not supported.") from exc
    task = tasks.update_task_status(
        db, actor=ctx.member, task=tasks.get_task(db, ctx.cohort, task_id), status=parsed
    )
    db.commit()
    return render(
        request,
        "fragments/task_row.html",
        {"view": tasks.build_view(task, viewer=ctx.member), "current_user": ctx.user},
    )


# ---------------------------------------------------------------------------
# Pinned resources (the fourth "turn into" action)
# ---------------------------------------------------------------------------


@router.post("/messages/{message_id}/pin-resource", summary="Pin a message as a resource")
def pin_resource(db: DbDep, ctx: PageCohort, message_id: uuid.UUID) -> Response:
    message = messages.get_visible_message(db, message_id=message_id, actor=ctx.member)
    messages.pin_message(db, actor=ctx.member, message=message)
    db.commit()
    channel = (
        channels.get_channel(db, ctx.cohort, message.channel_id)
        if message.channel_id
        else None
    )
    destination = f"/channels/{channel.slug}" if channel else "/"
    return RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)


def _member_or_none(db: DbDep, value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        user = accounts.require_user(db, uuid.UUID(value))
    except (ValueError, EmberError):
        return None
    return user.id
