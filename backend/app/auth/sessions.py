"""Server-side session management.

Sessions are opaque random tokens.  Only the SHA-256 hash of a token is stored
in PostgreSQL, so a database read cannot be replayed as a login.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.core.security import generate_token, hash_token
from app.db.base import rows_affected, utcnow
from app.models.user import User, UserSession

# Refresh ``last_seen_at`` at most this often to avoid a write on every request.
LAST_SEEN_REFRESH = dt.timedelta(minutes=5)


def create_session(
    db: DbSession,
    user: User,
    *,
    user_agent: str | None = None,
    ip_address: str | None = None,
) -> tuple[UserSession, str]:
    """Create a session row and return it with the raw (uncstored) token."""

    raw_token = generate_token()
    session = UserSession(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=utcnow() + dt.timedelta(days=settings.session_max_age_days),
        user_agent=(user_agent or "")[:255] or None,
        ip_address=(ip_address or "")[:64] or None,
    )
    db.add(session)
    # Presence should light up the instant someone signs in, not only after the
    # first throttled refresh five minutes later.
    user.last_active_at = utcnow()
    db.flush()
    return session, raw_token


def resolve_session(db: DbSession, raw_token: str | None) -> tuple[UserSession, User] | None:
    """Return the live session and its user, or ``None`` if invalid/expired."""

    if not raw_token:
        return None
    token_hash = hash_token(raw_token)
    row = db.execute(
        select(UserSession, User)
        .join(User, User.id == UserSession.user_id)
        .where(UserSession.token_hash == token_hash)
    ).first()
    if row is None:
        return None
    session, user = row
    now = utcnow()
    if session.revoked_at is not None or session.expires_at <= now or not user.is_active:
        return None
    if now - session.last_seen_at > LAST_SEEN_REFRESH:
        session.last_seen_at = now
        # Denormalised onto the user so presence is one indexed read per member.
        user.last_active_at = now
        db.flush()
    return session, user


def set_active_cohort(
    db: DbSession, session: UserSession, cohort_id: uuid.UUID | None
) -> None:
    """Remember which cohort this browser session is working in."""

    session.active_cohort_id = cohort_id
    db.flush()


def revoke_session(db: DbSession, session_id: uuid.UUID) -> None:
    db.execute(
        update(UserSession)
        .where(UserSession.id == session_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )


def revoke_all_sessions_for_user(db: DbSession, user_id: uuid.UUID) -> int:
    result = db.execute(
        update(UserSession)
        .where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
        .values(revoked_at=utcnow())
    )
    return rows_affected(result)


def purge_expired_sessions(db: DbSession) -> int:
    """Remove sessions that expired more than 30 days ago."""

    cutoff = utcnow() - dt.timedelta(days=30)
    result = db.execute(delete(UserSession).where(UserSession.expires_at < cutoff))
    return rows_affected(result)


def cookie_kwargs() -> dict[str, object]:
    """Cookie attributes used for both setting and clearing the session cookie."""

    kwargs: dict[str, object] = {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": settings.cookie_samesite,
        "path": "/",
    }
    if settings.session_cookie_domain:
        kwargs["domain"] = settings.session_cookie_domain
    return kwargs
