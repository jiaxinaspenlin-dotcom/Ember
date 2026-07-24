"""FastAPI dependencies: database sessions, current user, cohort scoping."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query, Request
from sqlalchemy.orm import Session as DbSession

from app.auth import sessions
from app.core.config import settings
from app.core.errors import (
    AuthenticationError,
    EmberError,
    NotFoundError,
    PermissionDeniedError,
)
from app.db.session import SessionLocal
from app.models.cohort import Cohort, CohortMembership
from app.models.user import User, UserSession
from app.services import accounts, cohorts


def get_db() -> Iterator[DbSession]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


DbDep = Annotated[DbSession, Depends(get_db)]


def client_ip(request: Request) -> str | None:
    """Resolve the caller's address.

    ``X-Forwarded-For`` is attacker-controlled unless a trusted proxy sets it,
    and it feeds the rate limiter -- so it is only honoured when
    ``TRUST_PROXY_HEADERS`` says a proxy is actually in front of us.
    """

    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()[:64]
    return request.client.host if request.client else None


@dataclass(slots=True)
class AuthContext:
    """The authenticated principal for a request (identity only, no cohort)."""

    user: User
    session: UserSession

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id


def get_optional_auth(request: Request, db: DbDep) -> AuthContext | None:
    """Resolve the session cookie, or ``None`` when there is no valid session."""

    raw_token = request.cookies.get(settings.session_cookie_name)
    resolved = sessions.resolve_session(db, raw_token)
    if resolved is None:
        return None
    session, user = resolved
    context = AuthContext(user=user, session=session)
    request.state.auth = context
    return context


OptionalAuthDep = Annotated[AuthContext | None, Depends(get_optional_auth)]


def get_auth(request: Request, auth: OptionalAuthDep) -> AuthContext:
    """Require a valid session, and a confirmed address when that is required."""

    if auth is None:
        raise AuthenticationError(
            "Your session expired. Please sign in again.", code="SESSION_EXPIRED"
        )
    # The auth endpoints stay reachable so an unverified user can resend the
    # email, sign out, or complete verification.
    if not request.url.path.startswith("/api/auth/") and accounts.email_verification_pending(
        auth.user
    ):
        raise PermissionDeniedError(
            "Confirm your email address to continue.", code="EMAIL_NOT_VERIFIED"
        )
    return auth


AuthDep = Annotated[AuthContext, Depends(get_auth)]


def get_current_user(auth: AuthDep) -> User:
    return auth.user


CurrentUser = Annotated[User, Depends(get_current_user)]


# ---------------------------------------------------------------------------
# Cohort scoping
# ---------------------------------------------------------------------------


class NoActiveCohortError(EmberError):
    """Raised when a request needs a cohort but the user has not chosen one."""

    status_code = 409
    code = "NO_ACTIVE_COHORT"


@dataclass(slots=True)
class CohortContext:
    """The authenticated principal *within* a cohort.

    ``member`` is the :class:`CohortMembership`, which carries the per-cohort
    role and profile. Every scoped service is driven by ``cohort_id`` and
    ``member`` from here, so isolation is enforced uniformly.
    """

    user: User
    session: UserSession
    cohort: Cohort
    member: CohortMembership

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def cohort_id(self) -> uuid.UUID:
        return self.cohort.id

    @property
    def is_admin(self) -> bool:
        return self.member.is_admin


def _resolve_active_membership(db: DbSession, auth: AuthContext) -> CohortMembership | None:
    """Pick the membership this request operates in.

    Prefers the session's remembered cohort; falls back to the user's only
    cohort so a single-cohort user never has to choose.
    """

    active_id = auth.session.active_cohort_id
    if active_id is not None:
        membership = cohorts.get_membership(db, cohort_id=active_id, user_id=auth.user_id)
        if membership is not None:
            return membership
    # Stale or unset: if they belong to exactly one cohort, use it and remember.
    items = cohorts.list_user_cohorts(db, user=auth.user)
    if len(items) == 1:
        sessions.set_active_cohort(db, auth.session, items[0].cohort.id)
        db.commit()
        return items[0].membership
    return None


def get_cohort_context(request: Request, db: DbDep, auth: AuthDep) -> CohortContext:
    """Require an active cohort. Raises when the user has not chosen one."""

    membership = _resolve_active_membership(db, auth)
    if membership is None:
        raise NoActiveCohortError(
            "Choose a cohort to continue.", code="NO_ACTIVE_COHORT"
        )
    context = CohortContext(
        user=auth.user, session=auth.session, cohort=membership.cohort, member=membership
    )
    request.state.cohort = context
    return context


CohortDep = Annotated[CohortContext, Depends(get_cohort_context)]


def get_cohort_admin(ctx: CohortDep) -> CohortContext:
    if not ctx.is_admin:
        raise PermissionDeniedError("This action requires cohort admin access.")
    return ctx


AdminCohortDep = Annotated[CohortContext, Depends(get_cohort_admin)]


@dataclass(slots=True)
class Pagination:
    limit: int
    offset: int


def get_pagination(
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0, le=100_000)] = 0,
) -> Pagination:
    return Pagination(limit=min(limit, settings.max_page_size), offset=offset)


PaginationDep = Annotated[Pagination, Depends(get_pagination)]


def parse_uuid(value: str, *, field: str = "id") -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise NotFoundError(f"Invalid {field}.", code="INVALID_IDENTIFIER") from exc
