"""Test fixtures.

Tests run against a real PostgreSQL database (``ember_test`` by default) so the
constraints, indexes and full-text search we rely on are actually exercised.

The database starts **empty** for every test: no seed data, no demo accounts,
no fixtures that pretend to be application content.  Any user, cohort or
channel a test needs, it creates through the real service layer or the real
API.

Multi-tenancy note
------------------
Every principal belongs to a cohort. ``make_user`` enrols each user in one
shared per-test cohort so members can interact, and returns a :class:`Principal`
-- a thin wrapper that is *both* the ``User`` (``.id``, ``.email`` ...) and its
``CohortMembership`` (the ``actor``/``author``/``viewer`` a service expects).
Reach either side explicitly with ``.user`` and ``.membership``.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator

import pytest

# Point the application at the test database *before* app modules import settings.
os.environ.setdefault(
    "DATABASE_URL",
    os.environ.get("TEST_DATABASE_URL", "postgresql+psycopg://localhost:5432/ember_test"),
)
os.environ["ENVIRONMENT"] = "test"
os.environ.setdefault("SESSION_SECRET", "test-session-secret-value-0123456789")
os.environ.setdefault("ADMIN_EMAILS", "")
os.environ.setdefault("GITHUB_CLIENT_ID", "test-client-id")
os.environ.setdefault("GITHUB_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("EMAIL_BACKEND", "console")
os.environ.setdefault("ADMIN_GITHUB_USERNAMES", "")
# Open-join on by default (the demo behaviour); individual tests override it.
os.environ.setdefault("COHORT_OPEN_JOIN", "true")

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session as DbSession

from app.auth import sessions
from app.core.enums import UserRole
from app.db.session import SessionLocal, engine
from app.main import app
from app.models import Base
from app.models.cohort import Cohort, CohortMembership
from app.models.user import User
from app.services import accounts, channels, cohorts

TABLES_IN_TRUNCATION_ORDER = [table.name for table in reversed(Base.metadata.sorted_tables)]


@pytest.fixture(scope="session", autouse=True)
def _create_schema() -> Iterator[None]:
    """Build the schema once for the whole session."""

    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(autouse=True)
def _empty_database() -> Iterator[None]:
    """Guarantee every test starts with zero rows in every table."""

    with engine.begin() as connection:
        connection.execute(
            text(f"TRUNCATE {', '.join(TABLES_IN_TRUNCATION_ORDER)} RESTART IDENTITY CASCADE")
        )
    yield


@pytest.fixture
def db() -> Iterator[DbSession]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------------------
# The test principal: a User + their CohortMembership, usable as either
# ---------------------------------------------------------------------------


class Principal:
    """A signed-up user together with their membership in one cohort.

    Pass it wherever a service wants an ``actor``/``author``/``viewer``/
    ``creator``/``requester`` (it exposes the membership protocol), and read
    ``.id``/``.email``/``.display_name`` for the identity. ``.user`` and
    ``.membership`` reach the underlying ORM objects when a call specifically
    needs one (``membership=``, ``target=``, ``invitee=`` ...).
    """

    __test__ = False

    def __init__(self, user: User, membership: CohortMembership) -> None:
        self.user = user
        self.membership = membership

    # -- membership protocol (what services read off an ``actor``) --
    @property
    def user_id(self) -> uuid.UUID:
        return self.membership.user_id

    @property
    def cohort_id(self) -> uuid.UUID:
        return self.membership.cohort_id

    @property
    def cohort(self) -> Cohort:
        return self.membership.cohort

    @property
    def is_admin(self) -> bool:
        return self.membership.is_admin

    @property
    def role(self) -> UserRole:
        return self.membership.role

    @property
    def skill_names(self) -> list[str]:
        return self.membership.skill_names

    # -- identity --
    @property
    def id(self) -> uuid.UUID:
        return self.user.id

    @property
    def email(self) -> str | None:
        return self.user.email

    @property
    def display_name(self) -> str:
        return self.user.display_name

    @property
    def avatar_url(self) -> str | None:
        return self.user.avatar_url

    @property
    def email_verified(self) -> bool:
        return self.user.email_verified

    @property
    def is_active(self) -> bool:
        return self.user.is_active

    @property
    def created_at(self):
        return self.user.created_at


# ---------------------------------------------------------------------------
# Helpers that build real accounts through the real code paths
# ---------------------------------------------------------------------------

PASSWORD = "correct-horse-9-battery"


def _unique_email(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}@embercohort.dev"


def _bare_cohort(db: DbSession, name: str = "Test Cohort") -> Cohort:
    """An empty cohort with no creator, so a test's cohort contains exactly the
    users that test enrols -- and nothing inflates the ``users`` count."""

    cohort = Cohort(
        slug=f"test-{uuid.uuid4().hex[:10]}",
        name=name,
        created_by_id=None,
    )
    db.add(cohort)
    db.flush()
    return cohort


@pytest.fixture
def make_cohort(db: DbSession) -> Callable[..., Cohort]:
    """Create an independent, empty cohort (for cross-cohort isolation tests)."""

    def _make(name: str = "Another Cohort") -> Cohort:
        cohort = _bare_cohort(db, name)
        db.commit()
        return cohort

    return _make


@pytest.fixture
def default_cohort(db: DbSession) -> Cohort:
    """The shared cohort that ``make_user`` enrols into by default."""

    cohort = _bare_cohort(db)
    db.commit()
    return cohort


@pytest.fixture
def make_user(db: DbSession, default_cohort: Cohort):  # type: ignore[no-untyped-def]
    """Create a real account and enrol it in a cohort (the shared one by default)."""

    def _make(
        display_name: str = "Test Member",
        *,
        email: str | None = None,
        admin: bool = False,
        password: str = PASSWORD,
        cohort: Cohort | None = None,
        profile_completed: bool = True,
    ) -> Principal:
        user = accounts.register_with_password(
            db,
            email=email or _unique_email(display_name.split()[0].lower()),
            password=password,
            display_name=display_name,
        )
        membership = cohorts._add_membership(
            db,
            cohort=cohort or default_cohort,
            user=user,
            role=UserRole.ADMIN if admin else UserRole.MEMBER,
        )
        membership.profile_completed = profile_completed
        db.commit()
        return Principal(user, membership)

    return _make


@pytest.fixture
def enroll(db: DbSession):  # type: ignore[no-untyped-def]
    """Add an existing user to another cohort. Returns the new Principal."""

    def _enroll(user: User, cohort: Cohort, *, admin: bool = False) -> Principal:
        membership = cohorts._add_membership(
            db,
            cohort=cohort,
            user=user,
            role=UserRole.ADMIN if admin else UserRole.MEMBER,
        )
        membership.profile_completed = True
        db.commit()
        return Principal(user, membership)

    return _enroll


@pytest.fixture
def sign_in(client: TestClient):  # type: ignore[no-untyped-def]
    """Sign a principal in on the shared TestClient and return the client.

    ``cohort`` selects which cohort to make active when the user has more than
    one; with a single cohort it is auto-selected by the app.
    """

    def _sign_in(
        user: Principal | User, password: str = PASSWORD, *, cohort: Cohort | None = None
    ) -> TestClient:
        response = client.post(
            "/api/auth/login", json={"email": user.email, "password": password}
        )
        assert response.status_code == 200, response.text
        if cohort is not None:
            switch = client.post(f"/cohorts/{cohort.slug}/switch", follow_redirects=False)
            assert switch.status_code in (302, 303), switch.text
        return client

    return _sign_in


@pytest.fixture
def fresh_client():
    """A separate client with its own cookie jar (simulates another browser)."""

    def _make() -> TestClient:
        return TestClient(app)

    return _make


@pytest.fixture
def make_channel(db: DbSession):  # type: ignore[no-untyped-def]
    def _make(admin: Principal, name: str = "General"):  # type: ignore[no-untyped-def]
        channel = channels.create_channel(db, actor=admin.membership, name=name)
        db.commit()
        return channel

    return _make


@pytest.fixture
def captured_email(monkeypatch):
    """Capture every email the application sends, instead of delivering it."""

    from app.core import mailer

    sent: list[mailer.Email] = []

    def _capture(email: mailer.Email) -> mailer.DeliveryResult:
        sent.append(email)
        return mailer.DeliveryResult(delivered=False, backend="console")

    monkeypatch.setattr(mailer, "send", _capture)
    return sent


# Re-exported so tests can set an active cohort on a session directly.
set_active_cohort = sessions.set_active_cohort
