"""Kudos (shout-outs) and daily check-ins."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Form, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session as DbSession

from app.api.dependencies import CohortContext, DbDep
from app.core.errors import EmberError
from app.models.user import User
from app.services import community, profiles
from app.web.deps import PageCohort, page_context
from app.web.templating import render

router = APIRouter(tags=["web-community"])


def _cohort_members(db: DbSession, ctx: CohortContext) -> list[User]:
    rows, _ = profiles.list_directory(
        db, cohort=ctx.cohort, filters=profiles.DirectoryFilters(), exclude_user_id=ctx.user_id
    )
    return [m.user for m in rows]


# ---------------------------------------------------------------------------
# Kudos
# ---------------------------------------------------------------------------


@router.get("/kudos", response_class=HTMLResponse, summary="Kudos wall")
def kudos_wall(request: Request, db: DbDep, ctx: PageCohort) -> Response:
    return render(
        request,
        "pages/kudos.html",
        page_context(
            db,
            ctx,
            kudos=community.list_recent_kudos(db, cohort=ctx.cohort, limit=50),
            members=_cohort_members(db, ctx),
            active_nav="kudos",
        ),
    )


@router.post("/kudos", summary="Give kudos")
def give_kudos(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    to_user_id: Annotated[uuid.UUID, Form()],
    message: Annotated[str, Form()],
) -> Response:
    try:
        community.give_kudos(db, actor=ctx.member, to_user_id=to_user_id, message=message)
        db.commit()
    except EmberError as exc:
        db.rollback()
        return render(
            request,
            "pages/kudos.html",
            page_context(
                db,
                ctx,
                kudos=community.list_recent_kudos(db, cohort=ctx.cohort, limit=50),
                members=_cohort_members(db, ctx),
                error_message=exc.message,
                active_nav="kudos",
            ),
            status_code=exc.status_code,
        )
    return RedirectResponse("/kudos?given=1", status_code=status.HTTP_303_SEE_OTHER)


# ---------------------------------------------------------------------------
# Daily check-in
# ---------------------------------------------------------------------------


@router.get("/check-in", response_class=HTMLResponse, summary="Daily check-ins")
def check_in_feed(request: Request, db: DbDep, ctx: PageCohort) -> Response:
    return render(
        request,
        "pages/check_in.html",
        page_context(
            db,
            ctx,
            check_ins=community.list_recent_check_ins(db, cohort=ctx.cohort, limit=50),
            todays=community.todays_check_in(
                db, cohort_id=ctx.cohort_id, user_id=ctx.user_id
            ),
            active_nav="checkin",
        ),
    )


@router.post("/check-in", summary="Post a check-in")
def post_check_in(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    body: Annotated[str, Form()],
) -> Response:
    try:
        community.post_check_in(db, actor=ctx.member, body=body)
        db.commit()
    except EmberError as exc:
        db.rollback()
        return render(
            request,
            "pages/check_in.html",
            page_context(
                db,
                ctx,
                check_ins=community.list_recent_check_ins(db, cohort=ctx.cohort, limit=50),
                todays=community.todays_check_in(
                    db, cohort_id=ctx.cohort_id, user_id=ctx.user_id
                ),
                error_message=exc.message,
                active_nav="checkin",
            ),
            status_code=exc.status_code,
        )
    return RedirectResponse("/check-in?posted=1", status_code=status.HTTP_303_SEE_OTHER)
