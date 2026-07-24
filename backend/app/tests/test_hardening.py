"""Hardening checks: response headers, config guards, rate-limit budgets."""

from __future__ import annotations

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.config import INSECURE_SESSION_SECRET, Settings
from app.core.errors import RateLimitedError
from app.main import CONTENT_SECURITY_POLICY
from app.services import accounts

# ---------------------------------------------------------------------------
# Response headers
# ---------------------------------------------------------------------------


def test_security_headers_are_present_on_pages(client):
    response = client.get("/signin")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]


def test_security_headers_are_present_on_api_responses(client):
    response = client.get("/api/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" in response.headers


def test_csp_blocks_the_dangerous_sources(client):
    del client
    for clause in (
        "frame-ancestors 'none'",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "default-src 'self'",
    ):
        assert clause in CONTENT_SECURITY_POLICY


def test_hsts_is_not_sent_outside_production(client):
    assert "strict-transport-security" not in client.get("/signin").headers


# ---------------------------------------------------------------------------
# Production configuration guard
# ---------------------------------------------------------------------------


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "database_url": "postgresql+psycopg://localhost:5432/ember",
        "session_secret": "a" * 48,
        "backend_url": "https://ember.example",
        "frontend_url": "https://ember.example",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_production_refuses_the_placeholder_secret():
    with pytest.raises(PydanticValidationError, match="SESSION_SECRET"):
        _production_settings(session_secret=INSECURE_SESSION_SECRET)


def test_production_refuses_a_short_secret():
    with pytest.raises(PydanticValidationError, match="at least 32 characters"):
        _production_settings(session_secret="short-but-sixteen")


def test_production_requires_https_urls():
    with pytest.raises(PydanticValidationError, match="https"):
        _production_settings(backend_url="http://ember.example")


def test_valid_production_settings_are_accepted():
    settings = _production_settings()
    assert settings.is_production is True
    assert settings.cookie_secure is True


def test_development_settings_stay_permissive():
    settings = Settings(
        _env_file=None,
        environment="development",
        database_url="postgresql+psycopg://localhost:5432/ember_dev",
    )
    assert settings.is_production is False


def test_sqlite_is_rejected_outright():
    with pytest.raises(PydanticValidationError, match="PostgreSQL"):
        Settings(_env_file=None, database_url="sqlite:///./ember.db")


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def test_per_account_and_per_ip_budgets_are_independent(db, make_user, monkeypatch):
    """One person's failures must not lock out everyone behind the same proxy.

    Before this was split, the two budgets were OR-ed into a single count, so
    eight failures from anywhere would rate-limit the whole cohort whenever the
    app sat behind a reverse proxy (where every request shares one address).
    """

    from app.core.config import settings

    monkeypatch.setattr(settings, "login_max_attempts", 3)
    monkeypatch.setattr(settings, "login_max_attempts_per_ip", 100)

    victim = make_user("Victim Person", email="victim@embercohort.dev")
    bystander = make_user("Bystander Person", email="bystander@embercohort.dev")
    shared_ip = "10.0.0.7"

    for _ in range(3):
        with pytest.raises(Exception):  # noqa: B017 - AuthenticationError
            accounts.authenticate_with_password(
                db, email=victim.email, password="wrong-password-1", ip_address=shared_ip
            )
        db.commit()

    # The targeted account is now limited...
    with pytest.raises(RateLimitedError):
        accounts.authenticate_with_password(
            db, email=victim.email, password="wrong-password-1", ip_address=shared_ip
        )
    db.commit()

    # ...but a different member on the same address can still sign in.
    signed_in = accounts.authenticate_with_password(
        db,
        email=bystander.email,
        password="correct-horse-9-battery",
        ip_address=shared_ip,
    )
    assert signed_in.id == bystander.id


def test_per_ip_budget_still_stops_account_spraying(db, make_user, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "login_max_attempts", 50)
    monkeypatch.setattr(settings, "login_max_attempts_per_ip", 4)

    make_user("Sprayed One", email="one@embercohort.dev")
    make_user("Sprayed Two", email="two@embercohort.dev")
    attacker_ip = "203.0.113.9"

    for _ in range(4):
        with pytest.raises(Exception):  # noqa: B017 - AuthenticationError
            accounts.authenticate_with_password(
                db, email="one@embercohort.dev", password="wrong-1", ip_address=attacker_ip
            )
        db.commit()

    with pytest.raises(RateLimitedError):
        accounts.authenticate_with_password(
            db, email="two@embercohort.dev", password="wrong-1", ip_address=attacker_ip
        )


def test_signup_is_rate_limited_per_source_address(db, monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "signup_max_attempts_per_ip", 2)
    ip = "198.51.100.4"

    for index in range(2):
        accounts.register_with_password(
            db,
            email=f"bulk{index}@embercohort.dev",
            password="correct-horse-9",
            display_name=f"Bulk {index}",
            ip_address=ip,
        )
        db.commit()

    with pytest.raises(RateLimitedError):
        accounts.register_with_password(
            db,
            email="bulk-too-many@embercohort.dev",
            password="correct-horse-9",
            display_name="One Too Many",
            ip_address=ip,
        )


def test_failed_signup_counts_towards_the_limit(db, monkeypatch):
    """Otherwise the 409 response would be a free email-enumeration oracle."""

    from app.core.config import settings
    from app.core.errors import ConflictError

    monkeypatch.setattr(settings, "signup_max_attempts_per_ip", 3)
    ip = "198.51.100.55"

    accounts.register_with_password(
        db,
        email="taken@embercohort.dev",
        password="correct-horse-9",
        display_name="Taken Person",
        ip_address=ip,
    )
    db.commit()

    for _ in range(2):
        with pytest.raises(ConflictError):
            accounts.register_with_password(
                db,
                email="taken@embercohort.dev",
                password="correct-horse-9",
                display_name="Probe Person",
                ip_address=ip,
            )
        db.commit()

    with pytest.raises(RateLimitedError):
        accounts.register_with_password(
            db,
            email="another@embercohort.dev",
            password="correct-horse-9",
            display_name="Another Person",
            ip_address=ip,
        )


# ---------------------------------------------------------------------------
# Proxy header trust
# ---------------------------------------------------------------------------


def test_forwarded_header_is_ignored_unless_a_proxy_is_trusted():
    from unittest.mock import Mock

    from app.api.dependencies import client_ip
    from app.core.config import settings

    request = Mock()
    request.headers = {"x-forwarded-for": "1.2.3.4"}
    request.client = Mock(host="10.0.0.1")

    assert settings.trust_proxy_headers is False
    assert client_ip(request) == "10.0.0.1"  # spoofed header ignored

    settings.trust_proxy_headers = True
    try:
        assert client_ip(request) == "1.2.3.4"
    finally:
        settings.trust_proxy_headers = False


# ---------------------------------------------------------------------------
# Input caps
# ---------------------------------------------------------------------------


def test_oversized_password_is_rejected_before_hashing(client):
    """A megabyte password must not become an Argon2 CPU sink."""

    response = client.post(
        "/signin", data={"email": "someone@embercohort.dev", "password": "x" * 100_000}
    )
    assert response.status_code == 422


def test_oversized_signup_fields_are_rejected(client):
    response = client.post(
        "/signup",
        data={
            "display_name": "n" * 5_000,
            "email": "big@embercohort.dev",
            "password": "correct-horse-9",
        },
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Administrator lockout
# ---------------------------------------------------------------------------


def test_the_last_admin_cannot_be_demoted_through_the_api(client, make_user, sign_in, db):
    """Otherwise a cohort could be left with no administrator at all."""

    admin = make_user("Lonely Admin", admin=True)
    sign_in(admin)

    response = client.put(f"/api/admin/users/{admin.id}/role", json={"role": "member"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "COHORT_LAST_ADMIN"

    db.refresh(admin.membership)
    assert admin.membership.is_admin is True


def test_admin_can_still_demote_someone_else(client, make_user, sign_in, db):
    admin = make_user("Acting Admin", admin=True)
    other = make_user("Other Admin", admin=True)
    sign_in(admin)

    assert (
        client.put(f"/api/admin/users/{other.id}/role", json={"role": "member"}).status_code
        == 200
    )
    db.refresh(other.membership)
    assert other.membership.is_admin is False


def test_an_admin_can_demote_another_admin_in_the_cohort(db, make_user):
    """Cohort role management: an admin can demote a second admin."""

    from app.core.enums import UserRole
    from app.services import cohorts

    admin = make_user("Primary Admin", admin=True)
    other = make_user("Second Admin", admin=True)
    cohorts.set_member_role(
        db, actor=admin.membership, target=other.membership, role=UserRole.MEMBER
    )
    db.commit()
    assert other.membership.is_admin is False
