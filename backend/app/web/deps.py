"""Dependencies for HTML pages.

Unauthenticated page requests redirect to sign-in; signed-in users with no
active cohort redirect to the cohort picker.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session as DbSession

from app.api.dependencies import (
    AuthContext,
    CohortContext,
    DbDep,
    OptionalAuthDep,
    _resolve_active_membership,
)
from app.core.errors import EmberError
from app.services import accounts
from app.web.templating import navigation_context


class RedirectToSignIn(EmberError):
    """Raised (and handled) when a page needs a signed-in user."""

    status_code = 401
    code = "SIGN_IN_REQUIRED"


class PageRedirect(Exception):
    """Signals that the page handler should redirect instead of render."""

    def __init__(self, location: str, status_code: int = 303) -> None:
        super().__init__(location)
        self.location = location
        self.status_code = status_code

    def response(self) -> RedirectResponse:
        return RedirectResponse(self.location, status_code=self.status_code)


# Pages an unverified account may still reach, so it can finish verifying or
# leave. Everything else redirects to the "confirm your email" page.
VERIFICATION_EXEMPT_PREFIXES = ("/verify-email", "/signout", "/signin", "/signup")
# Pages reachable without an active cohort (choose / create / join one, sign out).
COHORT_EXEMPT_PREFIXES = ("/cohorts", "/join", "/signout", "/verify-email", "/profile/complete")


def require_page_auth(request: Request, auth: OptionalAuthDep) -> AuthContext:
    if auth is None:
        target = request.url.path
        if request.url.query:
            target = f"{target}?{request.url.query}"
        raise PageRedirect(f"/signin?next={target}")
    if not request.url.path.startswith(VERIFICATION_EXEMPT_PREFIXES) and (
        accounts.email_verification_pending(auth.user)
    ):
        raise PageRedirect("/verify-email")
    return auth


PageAuth = Annotated[AuthContext, Depends(require_page_auth)]


def require_page_cohort(request: Request, db: DbDep, auth: PageAuth) -> CohortContext:
    """Resolve the active-cohort context for a page, or send them to the picker."""

    membership = _resolve_active_membership(db, auth)
    if membership is None:
        raise PageRedirect("/cohorts")
    context = CohortContext(
        user=auth.user, session=auth.session, cohort=membership.cohort, member=membership
    )
    request.state.cohort = context
    return context


PageCohort = Annotated[CohortContext, Depends(require_page_cohort)]


def page_context(db: DbSession, ctx: CohortContext, **extra: Any) -> dict[str, Any]:
    """Base context shared by every signed-in, cohort-scoped page."""

    context: dict[str, Any] = {
        "current_user": ctx.user,
        "cohort": ctx.cohort,
        "membership": ctx.member,
        "is_admin": ctx.is_admin,
    }
    context.update(navigation_context(db, ctx))
    context.update(extra)
    return context


def hx_redirect(location: str) -> dict[str, str]:
    """Header that tells HTMX to perform a client-side redirect."""

    return {"HX-Redirect": location}


def hx_refresh() -> dict[str, str]:
    return {"HX-Refresh": "true"}


__all__ = [
    "DbDep",
    "PageAuth",
    "PageCohort",
    "PageRedirect",
    "hx_redirect",
    "hx_refresh",
    "page_context",
]
