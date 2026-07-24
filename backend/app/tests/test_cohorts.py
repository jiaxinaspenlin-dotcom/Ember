"""Cohort lifecycle: creation limits and boot-time database resilience."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import OperationalError

from app.core.config import settings
from app.core.errors import ConflictError
from app.db import session as db_session
from app.services import cohorts


def test_cohort_creation_is_capped_per_user(db, make_user, monkeypatch):
    monkeypatch.setattr(settings, "max_cohorts_created_per_user", 2)
    creator = make_user("Prolific Founder").user

    cohorts.create_cohort(db, creator=creator, name="First Cohort")
    cohorts.create_cohort(db, creator=creator, name="Second Cohort")
    db.commit()

    with pytest.raises(ConflictError) as exc:
        cohorts.create_cohort(db, creator=creator, name="Third Cohort")
    assert exc.value.code == "COHORT_CREATE_LIMIT"
    assert exc.value.status_code == 409


def test_cohort_creation_cap_can_be_disabled(db, make_user, monkeypatch):
    monkeypatch.setattr(settings, "max_cohorts_created_per_user", 0)
    creator = make_user("Unlimited Founder").user
    for i in range(5):
        cohorts.create_cohort(db, creator=creator, name=f"Cohort {i}")
    db.commit()  # no exception


def test_verify_database_succeeds_against_a_live_database():
    # The test database is up, so this returns without raising.
    db_session.verify_database(max_attempts=1)


def test_verify_database_retries_then_raises_when_down(monkeypatch):
    """A database that never answers fails loudly after the bounded attempts,
    instead of the app crashing on the very first blip."""

    calls = {"connect": 0, "sleep": 0}

    def boom(*_args, **_kwargs):
        calls["connect"] += 1
        raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    def fake_sleep(_seconds):
        calls["sleep"] += 1

    monkeypatch.setattr(db_session.engine, "connect", boom)
    monkeypatch.setattr(db_session.engine, "dispose", lambda *a, **k: None)
    monkeypatch.setattr("app.db.session.time.sleep", fake_sleep)

    with pytest.raises(OperationalError):
        db_session.verify_database(max_attempts=3, backoff_seconds=0)

    assert calls["connect"] == 3  # tried the full budget
    assert calls["sleep"] == 2  # slept between attempts, not after the last
