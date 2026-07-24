"""Home dashboard, notifications and announcements pages."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.dependencies import DbDep
from app.auth import permissions
from app.core.enums import Priority
from app.core.errors import EmberError
from app.services import announcements, community, dashboard, notifications
from app.web.deps import PageCohort, page_context
from app.web.templating import render

router = APIRouter(tags=["web"])


@router.get("/", response_class=HTMLResponse, summary="Home dashboard")
def home(request: Request, db: DbDep, ctx: PageCohort) -> Response:
    if not ctx.member.profile_completed:
        return RedirectResponse("/profile/complete", status_code=status.HTTP_303_SEE_OTHER)
    summary = dashboard.build_summary(db, ctx=ctx)
    pulse = community.compute_pulse(db, cohort=ctx.cohort)
    online_now = community.count_online(db, cohort=ctx.cohort)
    return render(
        request,
        "pages/home.html",
        page_context(
            db, ctx, summary=summary, pulse=pulse, online_now=online_now, active_nav="home"
        ),
    )


@router.get("/notifications", response_class=HTMLResponse, summary="Notifications")
def notifications_page(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    unread_only: bool = False,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    items, total = notifications.list_for_user(
        db, cohort_id=ctx.cohort_id, user=ctx.user, unread_only=unread_only, limit=50, offset=offset
    )
    return render(
        request,
        "pages/notifications.html",
        page_context(
            db,
            ctx,
            notifications=items,
            total=total,
            unread_only=unread_only,
            offset=offset,
            active_nav="notifications",
        ),
    )


@router.post("/notifications/{notification_id}/read", summary="Mark a notification read")
def mark_notification_read(
    notification_id: uuid.UUID, db: DbDep, ctx: PageCohort
) -> Response:
    notification = notifications.mark_notification_read(
        db, user=ctx.user, notification_id=notification_id
    )
    db.commit()
    return RedirectResponse(notification.link_path, status_code=status.HTTP_303_SEE_OTHER)


@router.post("/notifications/read-all", summary="Mark all notifications read")
def mark_all_read(db: DbDep, ctx: PageCohort) -> Response:
    notifications.mark_all_read(db, cohort_id=ctx.cohort_id, user=ctx.user)
    db.commit()
    return RedirectResponse("/notifications", status_code=status.HTTP_303_SEE_OTHER)


@router.get(
    "/hx/notifications/badge", response_class=HTMLResponse, summary="Unread badge (polled)"
)
def notification_badge(request: Request, db: DbDep, ctx: PageCohort) -> Response:
    count = notifications.unread_count(db, cohort_id=ctx.cohort_id, user_id=ctx.user_id)
    return render(request, "fragments/notification_badge.html", {"count": count})


@router.get("/announcements", response_class=HTMLResponse, summary="Announcements")
def announcements_page(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    include_expired: bool = False,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    items, total = announcements.list_announcements(
        db, cohort=ctx.cohort, include_expired=include_expired, limit=25, offset=offset
    )
    return render(
        request,
        "pages/announcements.html",
        page_context(
            db,
            ctx,
            announcements=items,
            total=total,
            include_expired=include_expired,
            offset=offset,
            active_nav="announcements",
        ),
    )


@router.post("/announcements", response_class=HTMLResponse, summary="Publish an announcement")
def create_announcement(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    title: Annotated[str, Form()],
    body: Annotated[str, Form()],
    priority: Annotated[str, Form()] = Priority.NORMAL.value,
    is_pinned: Annotated[str | None, Form()] = None,
) -> Response:
    permissions.require_announcement_management(ctx.member)
    try:
        announcements.create_announcement(
            db,
            author=ctx.member,
            title=title,
            body=body,
            priority=Priority(priority),
            is_pinned=is_pinned is not None,
        )
        db.commit()
    except (EmberError, ValueError) as exc:
        db.rollback()
        items, total = announcements.list_announcements(db, cohort=ctx.cohort, limit=25)
        message = exc.message if isinstance(exc, EmberError) else "Choose a valid priority."
        return render(
            request,
            "pages/announcements.html",
            page_context(
                db,
                ctx,
                announcements=items,
                total=total,
                include_expired=False,
                offset=0,
                error_message=message,
                active_nav="announcements",
            ),
            status_code=422,
        )
    return RedirectResponse("/announcements", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/announcements/{announcement_id}/delete", summary="Delete an announcement")
def delete_announcement(announcement_id: uuid.UUID, db: DbDep, ctx: PageCohort) -> Response:
    announcements.delete_announcement(
        db,
        actor=ctx.member,
        announcement=announcements.get_announcement(db, ctx.cohort, announcement_id),
    )
    db.commit()
    return RedirectResponse("/announcements", status_code=status.HTTP_303_SEE_OTHER)
