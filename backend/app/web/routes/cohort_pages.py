"""Cohort picker, creation, joining and the workspace switcher."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.dependencies import DbDep
from app.auth import sessions
from app.core.config import settings
from app.core.errors import EmberError
from app.services import cohorts
from app.web.deps import PageAuth, PageCohort
from app.web.templating import render

router = APIRouter(tags=["web-cohorts"])


@router.get("/cohorts", response_class=HTMLResponse, summary="Choose or create a cohort")
def cohorts_page(request: Request, db: DbDep, auth: PageAuth) -> Response:
    mine = cohorts.list_user_cohorts(db, user=auth.user)
    joinable = cohorts.list_joinable(db, user=auth.user, limit=100)
    return render(
        request,
        "pages/cohorts.html",
        {
            "current_user": auth.user,
            "my_cohorts": mine,
            "joinable_cohorts": joinable,
            "open_join": settings.cohort_open_join,
        },
    )


@router.post("/cohorts", summary="Create a cohort")
def create_cohort(
    request: Request,
    db: DbDep,
    auth: PageAuth,
    name: Annotated[str, Form(max_length=80)],
    description: Annotated[str, Form(max_length=300)] = "",
) -> Response:
    try:
        membership = cohorts.create_cohort(
            db, creator=auth.user, name=name, description=description
        )
        sessions.set_active_cohort(db, auth.session, membership.cohort_id)
        db.commit()
    except EmberError as exc:
        db.rollback()
        mine = cohorts.list_user_cohorts(db, user=auth.user)
        return render(
            request,
            "pages/cohorts.html",
            {
                "current_user": auth.user,
                "my_cohorts": mine,
                "joinable_cohorts": cohorts.list_joinable(db, user=auth.user),
                "open_join": settings.cohort_open_join,
                "error_message": exc.message,
            },
            status_code=exc.status_code,
        )
    return RedirectResponse("/profile/complete", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/cohorts/{slug}/join", summary="Join a discoverable cohort")
def join_cohort(request: Request, db: DbDep, auth: PageAuth, slug: str) -> Response:
    del request
    cohort = cohorts.get_cohort_by_slug(db, slug)
    membership = cohorts.open_join(db, user=auth.user, cohort=cohort)
    sessions.set_active_cohort(db, auth.session, membership.cohort_id)
    db.commit()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/cohorts/{slug}/switch", summary="Switch the active cohort")
def switch_cohort(db: DbDep, auth: PageAuth, slug: str) -> Response:
    cohort = cohorts.get_cohort_by_slug(db, slug)
    # Must be a member to switch into it.
    membership = cohorts.get_membership(db, cohort_id=cohort.id, user_id=auth.user_id)
    if membership is None:
        return RedirectResponse("/cohorts", status_code=status.HTTP_303_SEE_OTHER)
    sessions.set_active_cohort(db, auth.session, cohort.id)
    db.commit()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.get("/join/{invite_code}", summary="Join a cohort from an invite link")
def join_by_invite(db: DbDep, auth: PageAuth, invite_code: str) -> Response:
    membership = cohorts.join_by_invite(db, user=auth.user, invite_code=invite_code)
    sessions.set_active_cohort(db, auth.session, membership.cohort_id)
    db.commit()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/cohorts/leave", summary="Leave the active cohort")
def leave_cohort(db: DbDep, ctx: PageCohort) -> Response:
    cohorts.leave_cohort(db, membership=ctx.member)
    sessions.set_active_cohort(db, ctx.session, None)
    db.commit()
    return RedirectResponse("/cohorts", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Cohort settings (admin): invite link + roles, shown on the admin console.
# ---------------------------------------------------------------------------


@router.post("/cohort/invite-link", summary="Create or rotate the cohort invite link")
def create_cohort_invite(db: DbDep, ctx: PageCohort) -> Response:
    cohorts.generate_invite_code(db, actor=ctx.member, cohort=ctx.cohort)
    db.commit()
    return RedirectResponse("/admin#invite", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/cohort/invite-link/revoke", summary="Turn off the cohort invite link")
def revoke_cohort_invite(db: DbDep, ctx: PageCohort) -> Response:
    cohorts.revoke_invite_code(db, actor=ctx.member, cohort=ctx.cohort)
    db.commit()
    return RedirectResponse("/admin#invite", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/cohort/members/{user_id}/role", summary="Change a member's cohort role")
def set_member_role(
    db: DbDep, ctx: PageCohort, user_id: str, role: Annotated[str, Form()]
) -> Response:
    from app.core.enums import UserRole

    target = cohorts.get_membership(db, cohort_id=ctx.cohort_id, user_id=_as_uuid(user_id))
    if target is None:
        return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)
    cohorts.set_member_role(db, actor=ctx.member, target=target, role=UserRole(role))
    db.commit()
    return RedirectResponse("/admin", status_code=status.HTTP_303_SEE_OTHER)


def _as_uuid(value: str) -> uuid.UUID:
    return uuid.UUID(value)
