"""Database engine and session management."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.logging import get_logger

logger = get_logger("ember.db")

engine: Engine = create_engine(
    settings.database_url,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
    pool_recycle=1800,
    future=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
    expire_on_commit=False,
    future=True,
)


def verify_database(
    *,
    max_attempts: int | None = None,
    backoff_seconds: float | None = None,
) -> None:
    """Confirm the database answers, retrying with backoff before giving up.

    Called once at boot. A brief race (app container up before Postgres accepts
    connections) is waited out; a database that is genuinely down still raises
    after the bounded attempts, so a misconfigured deploy fails loudly.
    """

    attempts = max_attempts if max_attempts is not None else settings.db_connect_max_attempts
    backoff = (
        backoff_seconds if backoff_seconds is not None else settings.db_connect_backoff_seconds
    )
    attempts = max(1, attempts)
    last_error: SQLAlchemyError | None = None
    for attempt in range(1, attempts + 1):
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
            if attempt > 1:
                logger.info("Database reachable after %d attempt(s)", attempt)
            return
        except SQLAlchemyError as exc:
            last_error = exc
            if attempt < attempts:
                # Recycle any half-open pooled connections before waiting.
                engine.dispose()
                wait = backoff * attempt
                logger.warning(
                    "Database not ready (attempt %d/%d): %s -- retrying in %.1fs",
                    attempt,
                    attempts,
                    type(exc).__name__,
                    wait,
                )
                time.sleep(wait)
    assert last_error is not None
    raise last_error


@contextmanager
def session_scope() -> Iterator[Session]:
    """Provide a transactional scope for scripts and background work."""

    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
