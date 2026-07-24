"""Member directory, profile editing, search and cohort-admin pages."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.dependencies import DbDep
from app.auth import permissions
from app.core.config import settings
from app.core.enums import UserRole, WorkingStatus
from app.core.errors import EmberError, NotFoundError, ValidationError
from app.search.queries import SearchFilters, SearchScope, search
from app.services import audit, channels, cohorts, direct_messages, profiles
from app.web.deps import PageCohort, page_context
from app.web.templating import render

router = APIRouter(tags=["web-directory"])


# ---------------------------------------------------------------------------
# Member directory
# ---------------------------------------------------------------------------


@router.get("/members", response_class=HTMLResponse, summary="Member directory")
def members_page(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    q: Annotated[str | None, Query(max_length=120)] = None,
    skill: Annotated[str | None, Query(max_length=60)] = None,
    working_status: str | None = None,
    available_only: bool = False,
    project_area: Annotated[str | None, Query(max_length=80)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    status_enum: WorkingStatus | None = None
    if working_status:
        try:
            status_enum = WorkingStatus(working_status)
        except ValueError as exc:
            raise ValidationError("That working status is not supported.") from exc

    rows, total = profiles.list_directory(
        db,
        cohort=ctx.cohort,
        filters=profiles.DirectoryFilters(
            query=q,
            skill=skill,
            working_status=status_enum,
            available_only=available_only,
            project_area=project_area,
        ),
        exclude_user_id=ctx.user_id,
        limit=50,
        offset=offset,
    )
    return render(
        request,
        "pages/members.html",
        page_context(
            db,
            ctx,
            members=rows,
            total=total,
            offset=offset,
            query=q or "",
            selected_skill=skill or "",
            selected_status=working_status or "",
            available_only=available_only,
            selected_area=project_area or "",
            all_skills=profiles.list_all_skills(db, cohort=ctx.cohort),
            all_areas=profiles.list_project_areas(db, cohort=ctx.cohort),
            active_nav="members",
        ),
    )


@router.get("/members/{user_id}", response_class=HTMLResponse, summary="A member profile")
def member_page(
    request: Request, db: DbDep, ctx: PageCohort, user_id: uuid.UUID
) -> Response:
    membership = cohorts.get_membership(db, cohort_id=ctx.cohort_id, user_id=user_id)
    if membership is None:
        raise NotFoundError("Member not found.", code="USER_NOT_FOUND")
    existing = direct_messages.find_conversation_between(
        db, cohort_id=ctx.cohort_id, user_a=ctx.user_id, user_b=membership.user_id
    )
    return render(
        request,
        "pages/member.html",
        page_context(
            db,
            ctx,
            member=membership.user,
            profile=membership,
            existing_conversation=existing,
            is_self=membership.user_id == ctx.user_id,
            active_nav="members",
        ),
    )


# ---------------------------------------------------------------------------
# Own profile
# ---------------------------------------------------------------------------


@router.get("/profile", response_class=HTMLResponse, summary="Your profile")
def profile_page(request: Request, db: DbDep, ctx: PageCohort) -> Response:
    return render(
        request,
        "pages/profile.html",
        page_context(db, ctx, profile=ctx.member, active_nav="profile"),
    )


@router.post("/profile", response_class=HTMLResponse, summary="Save your profile")
def save_profile(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    display_name: Annotated[str, Form()],
    avatar_url: Annotated[str, Form()] = "",
    bio: Annotated[str, Form()] = "",
    skills: Annotated[str, Form()] = "",
    current_project: Annotated[str, Form()] = "",
    project_area: Annotated[str, Form()] = "",
    working_status: Annotated[str, Form()] = WorkingStatus.BUILDING.value,
    available_to_help: Annotated[str | None, Form()] = None,
) -> Response:
    try:
        profiles.update_profile(
            db,
            membership=ctx.member,
            display_name=display_name,
            avatar_url=avatar_url,
            bio=bio,
            skills=[part.strip() for part in skills.split(",") if part.strip()],
            current_project=current_project,
            project_area=project_area,
            working_status=WorkingStatus(working_status),
            available_to_help=available_to_help is not None,
        )
        db.commit()
    except (EmberError, ValueError) as exc:
        db.rollback()
        message = exc.message if isinstance(exc, EmberError) else "Choose a valid status."
        return render(
            request,
            "pages/profile.html",
            page_context(
                db,
                ctx,
                profile=ctx.member,
                error_message=message,
                active_nav="profile",
            ),
            status_code=422,
        )
    return RedirectResponse("/profile?saved=1", status_code=status.HTTP_303_SEE_OTHER)


@router.post(
    "/hx/profile/working-status",
    response_class=HTMLResponse,
    summary="Change working status (HTMX)",
)
def set_working_status(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    working_status: Annotated[str, Form()],
) -> Response:
    try:
        parsed = WorkingStatus(working_status)
    except ValueError as exc:
        raise ValidationError("That working status is not supported.") from exc
    membership = profiles.set_working_status(db, membership=ctx.member, working_status=parsed)
    db.commit()
    return render(
        request,
        "fragments/working_status.html",
        {"profile": membership, "current_user": ctx.user},
    )


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


@router.get("/search", response_class=HTMLResponse, summary="Search")
def search_page(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    q: Annotated[str | None, Query(max_length=200)] = None,
    scope: str = SearchScope.ALL.value,
    channel_id: uuid.UUID | None = None,
    sender_id: uuid.UUID | None = None,
    date_from: Annotated[str | None, Query()] = None,
    date_to: Annotated[str | None, Query()] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Response:
    try:
        scope_enum = SearchScope(scope)
    except ValueError as exc:
        raise ValidationError("That search scope is not supported.") from exc

    response = None
    if q and q.strip():
        response = search(
            db,
            cohort_id=ctx.cohort_id,
            user=ctx.user,
            filters=SearchFilters(
                query=q,
                scope=scope_enum,
                channel_id=channel_id,
                sender_id=sender_id,
                date_from=_parse_date(date_from),
                date_to=_parse_date(date_to, end_of_day=True),
            ),
            limit=25,
            offset=offset,
        )

    channel_items, _ = channels.list_channels(
        db, cohort=ctx.cohort, user=ctx.user, include_archived=True, limit=100
    )
    members, _ = profiles.list_directory(
        db, cohort=ctx.cohort, filters=profiles.DirectoryFilters(), limit=100
    )
    return render(
        request,
        "pages/search.html",
        page_context(
            db,
            ctx,
            response=response,
            query=q or "",
            scope=scope_enum,
            selected_channel=channel_id,
            selected_sender=sender_id,
            date_from=date_from or "",
            date_to=date_to or "",
            offset=offset,
            all_channels=[item.channel for item in channel_items],
            all_members=[membership.user for membership in members],
            active_nav="search",
        ),
    )


def _parse_date(value: str | None, *, end_of_day: bool = False) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError("Enter dates as YYYY-MM-DD.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    if end_of_day and parsed.hour == 0 and parsed.minute == 0:
        parsed = parsed + dt.timedelta(hours=23, minutes=59, seconds=59)
    return parsed


# ---------------------------------------------------------------------------
# Cohort admin console
# ---------------------------------------------------------------------------


@router.get("/admin", response_class=HTMLResponse, summary="Cohort admin console")
def admin_page(request: Request, db: DbDep, ctx: PageCohort) -> Response:
    permissions.require_admin(ctx.member)
    from sqlalchemy import func, select

    from app.models.action import Decision, HelpRequest, Task
    from app.models.cohort import CohortMembership
    from app.models.message import Message

    cohort_id = ctx.cohort_id

    def count(model: Any) -> int:
        return int(
            db.scalar(
                select(func.count()).select_from(model).where(model.cohort_id == cohort_id)
            )
            or 0
        )

    stats = {
        "members": cohorts.member_count(db, cohort_id),
        "admins": int(
            db.scalar(
                select(func.count())
                .select_from(CohortMembership)
                .where(
                    CohortMembership.cohort_id == cohort_id,
                    CohortMembership.role == UserRole.ADMIN,
                )
            )
            or 0
        ),
        "channels": channels.channel_count(db, cohort=ctx.cohort),
        "archived_channels": channels.channel_count(db, cohort=ctx.cohort, include_archived=True)
        - channels.channel_count(db, cohort=ctx.cohort),
        "messages": count(Message),
        "help_requests": count(HelpRequest),
        "decisions": count(Decision),
        "tasks": count(Task),
    }
    members, _ = cohorts.list_members(db, cohort=ctx.cohort, limit=100)
    channel_items, _ = channels.list_channels(
        db, cohort=ctx.cohort, user=ctx.user, include_archived=True, limit=100
    )
    invite_url = None
    if ctx.cohort.invite_code:
        invite_url = (
            f"{settings.frontend_url.rstrip('/')}/join/{ctx.cohort.invite_code}"
        )
    return render(
        request,
        "pages/admin.html",
        page_context(
            db,
            ctx,
            stats=stats,
            members=members,
            channel_items=channel_items,
            cohort_invite_url=invite_url,
            open_join=settings.cohort_open_join,
            audit_events=audit.recent_events(db, cohort_id=cohort_id, limit=25),
            active_nav="admin",
        ),
    )


@router.post("/admin/users/{user_id}/role", summary="Change a member's role")
def change_role(
    db: DbDep, ctx: PageCohort, user_id: uuid.UUID, role: Annotated[str, Form()]
) -> Response:
    permissions.require_admin(ctx.member)
    try:
        parsed = UserRole(role)
    except ValueError as exc:
        raise ValidationError("That role is not supported.") from exc
    target = cohorts.get_membership(db, cohort_id=ctx.cohort_id, user_id=user_id)
    if target is None:
        raise NotFoundError("Member not found.", code="USER_NOT_FOUND")
    cohorts.set_member_role(db, actor=ctx.member, target=target, role=parsed)
    db.commit()
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
