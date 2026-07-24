"""Authentication: signup, hashing, login, logout, sessions, GitHub OAuth."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from sqlalchemy import select

from app.auth import passwords, sessions
from app.core.config import settings
from app.core.enums import UserRole
from app.core.errors import AuthenticationError, ConflictError, RateLimitedError, ValidationError
from app.db.base import utcnow
from app.models.cohort import CohortMembership
from app.models.user import OAuthAccount, PasswordCredential, User, UserSession
from app.services import accounts
from app.services.accounts import GitHubIdentity


def test_email_signup_creates_user_profile_and_credential(client, db):
    response = client.post(
        "/api/auth/signup",
        json={
            "email": "Reviewer@EmberCohort.DEV",
            "password": "correct-horse-9",
            "display_name": "Ada Reviewer",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()["user"]
    assert body["email"] == "reviewer@embercohort.dev"  # normalised
    # Identity is global now: a fresh account has no role and no cohort yet.
    assert "role" not in body

    user = db.scalar(select(User).where(User.email == "reviewer@embercohort.dev"))
    assert user is not None
    assert (
        db.scalar(select(CohortMembership).where(CohortMembership.user_id == user.id))
        is None
    )
    credential = db.scalar(
        select(PasswordCredential).where(PasswordCredential.user_id == user.id)
    )
    assert credential is not None
    assert credential.password_hash.startswith("$argon2")
    assert "correct-horse-9" not in credential.password_hash


def test_password_hash_never_exposed_in_responses(client):
    client.post(
        "/api/auth/signup",
        json={
            "email": "hidden@embercohort.dev",
            "password": "correct-horse-9",
            "display_name": "Hidden Fields",
        },
    )
    payload = client.get("/api/auth/me").text
    for forbidden in ("password", "hash", "token", "argon2"):
        assert forbidden not in payload.lower()


def test_password_verification_is_argon2_and_rejects_wrong_password():
    hashed = passwords.hash_password("correct-horse-9")
    assert hashed != "correct-horse-9"
    assert passwords.verify_password("correct-horse-9", hashed) is True
    assert passwords.verify_password("wrong-password-1", hashed) is False
    # A missing credential still performs a verification and returns False.
    assert passwords.verify_password("anything-1", None) is False


@pytest.mark.parametrize(
    "password",
    ["short1", "nodigitsatallhere", "1234567890", "  spaced  "],
)
def test_weak_passwords_are_rejected(password):
    with pytest.raises(ValidationError):
        passwords.validate_password_strength(password)


def test_duplicate_email_signup_is_rejected(client, db):
    payload = {
        "email": "dup@embercohort.dev",
        "password": "correct-horse-9",
        "display_name": "First Person",
    }
    assert client.post("/api/auth/signup", json=payload).status_code == 201
    second = client.post("/api/auth/signup", json=payload)
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
    assert db.scalar(select(User).where(User.email == "dup@embercohort.dev")) is not None


def test_login_success_and_failure_message_is_generic(client, make_user):
    user = make_user("Log In", email="login@embercohort.dev")
    ok = client.post(
        "/api/auth/login",
        json={"email": "login@embercohort.dev", "password": "correct-horse-9-battery"},
    )
    assert ok.status_code == 200
    assert ok.json()["user"]["id"] == str(user.id)

    bad = client.post(
        "/api/auth/login", json={"email": "login@embercohort.dev", "password": "wrong-password-1"}
    )
    assert bad.status_code == 401
    assert bad.json()["error"]["message"] == accounts.GENERIC_LOGIN_FAILURE


def test_login_does_not_reveal_whether_an_account_exists(client, make_user):
    make_user("Real Person", email="real@embercohort.dev")
    known = client.post(
        "/api/auth/login", json={"email": "real@embercohort.dev", "password": "wrong-password-1"}
    )
    unknown = client.post(
        "/api/auth/login", json={"email": "ghost@embercohort.dev", "password": "wrong-password-1"}
    )
    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json()


def test_login_is_rate_limited(client, make_user):
    make_user("Brute Force", email="brute@embercohort.dev")
    codes = []
    for _ in range(settings.login_max_attempts + 2):
        codes.append(
            client.post(
                "/api/auth/login",
                json={"email": "brute@embercohort.dev", "password": "wrong-password-1"},
            ).status_code
        )
    assert 429 in codes


def test_rate_limit_raised_from_service_layer(db, make_user):
    make_user("Service Brute", email="servicebrute@embercohort.dev")
    for _ in range(settings.login_max_attempts):
        with pytest.raises(AuthenticationError):
            accounts.authenticate_with_password(
                db, email="servicebrute@embercohort.dev", password="wrong-password-1"
            )
        db.commit()
    with pytest.raises(RateLimitedError):
        accounts.authenticate_with_password(
            db, email="servicebrute@embercohort.dev", password="wrong-password-1"
        )


def test_session_is_stored_server_side_and_only_hashed(client, make_user, db):
    user = make_user("Session Owner", email="session@embercohort.dev")
    client.post(
        "/api/auth/login",
        json={"email": user.email, "password": "correct-horse-9-battery"},
    )
    raw_cookie = client.cookies.get(settings.session_cookie_name)
    assert raw_cookie

    stored = db.scalars(select(UserSession).where(UserSession.user_id == user.id)).all()
    assert len(stored) == 1
    assert stored[0].token_hash != raw_cookie
    assert raw_cookie not in stored[0].token_hash


def test_logout_revokes_the_session_and_blocks_private_endpoints(client, make_user, db):
    user = make_user("Logout Person", email="logout@embercohort.dev")
    client.post(
        "/api/auth/login", json={"email": user.email, "password": "correct-horse-9-battery"}
    )
    assert client.get("/api/auth/me").status_code == 200

    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/notifications").status_code == 401

    session_row = db.scalars(select(UserSession).where(UserSession.user_id == user.id)).one()
    assert session_row.revoked_at is not None


def test_expired_session_returns_401(client, make_user, db):
    user = make_user("Expiring", email="expiring@embercohort.dev")
    client.post(
        "/api/auth/login", json={"email": user.email, "password": "correct-horse-9-battery"}
    )
    session_row = db.scalars(select(UserSession).where(UserSession.user_id == user.id)).one()
    session_row.expires_at = utcnow() - dt.timedelta(seconds=1)
    db.commit()

    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"


def test_anonymous_requests_are_blocked_everywhere(client):
    for path in (
        "/api/auth/me",
        "/api/channels",
        "/api/dashboard",
        "/api/notifications",
        "/api/direct-messages",
        "/api/tasks",
        "/api/decisions",
        "/api/help-requests",
        "/api/members",
        "/api/search?q=anything",
    ):
        assert client.get(path).status_code == 401, path


def test_session_survives_a_new_browser_with_the_same_cookie(client, make_user, fresh_client):
    user = make_user("Multi Device", email="multi@embercohort.dev")
    client.post(
        "/api/auth/login", json={"email": user.email, "password": "correct-horse-9-battery"}
    )
    cookie = client.cookies.get(settings.session_cookie_name)

    other = fresh_client()
    other.cookies.set(settings.session_cookie_name, cookie)
    assert other.get("/api/auth/me").json()["id"] == str(user.id)


def test_revoke_all_sessions_signs_out_every_device(client, make_user, fresh_client):
    user = make_user("Everywhere", email="everywhere@embercohort.dev")
    client.post(
        "/api/auth/login", json={"email": user.email, "password": "correct-horse-9-battery"}
    )
    second = fresh_client()
    second.post(
        "/api/auth/login", json={"email": user.email, "password": "correct-horse-9-battery"}
    )

    assert client.post("/api/auth/sessions/revoke-all").status_code == 200
    assert second.get("/api/auth/me").status_code == 401


# ---------------------------------------------------------------------------
# GitHub OAuth (the identity resolution layer; the HTTP calls are exercised
# separately by hand -- see docs/GITHUB_OAUTH.md)
# ---------------------------------------------------------------------------


def github_identity(**overrides: Any) -> GitHubIdentity:
    payload: dict[str, Any] = {
        "provider_account_id": "424242",
        "username": "octobuilder",
        "display_name": "Octo Builder",
        "avatar_url": "https://avatars.embercohort.dev/octobuilder.png",
        "email": "octo@embercohort.dev",
        "email_verified": True,
        "access_token": "gho_secret_value",
        "scopes": "read:user user:email",
    }
    payload.update(overrides)
    return GitHubIdentity(**payload)


def test_first_github_login_creates_user_and_oauth_account(db):
    user, created = accounts.resolve_github_user(db, github_identity())
    db.commit()

    assert created is True
    assert user.display_name == "Octo Builder"
    assert user.avatar_url == "https://avatars.embercohort.dev/octobuilder.png"
    # No cohort membership until they create or join one.
    assert (
        db.scalar(select(CohortMembership).where(CohortMembership.user_id == user.id))
        is None
    )

    link = db.scalar(select(OAuthAccount).where(OAuthAccount.user_id == user.id))
    assert link is not None
    assert link.provider == "github"
    assert link.provider_account_id == "424242"


def test_returning_github_login_reuses_the_same_account(db):
    first, created_first = accounts.resolve_github_user(db, github_identity())
    db.commit()
    second, created_second = accounts.resolve_github_user(db, github_identity())
    db.commit()

    assert created_first is True
    assert created_second is False
    assert first.id == second.id
    assert db.scalar(select(User.id).where(User.id == first.id)) is not None
    assert len(db.scalars(select(OAuthAccount)).all()) == 1


def test_github_login_does_not_overwrite_user_edited_profile_fields(db):
    user, _ = accounts.resolve_github_user(db, github_identity())
    db.commit()

    user.display_name = "Octo (they/them)"
    user.avatar_url = "https://cdn.embercohort.dev/custom.png"
    db.commit()

    accounts.resolve_github_user(
        db, github_identity(display_name="Renamed On GitHub", avatar_url="https://gh/new.png")
    )
    db.commit()
    db.refresh(user)

    assert user.display_name == "Octo (they/them)"
    assert user.avatar_url == "https://cdn.embercohort.dev/custom.png"


def test_github_login_without_an_email_still_works(db):
    user, created = accounts.resolve_github_user(
        db, github_identity(email=None, email_verified=False)
    )
    db.commit()
    assert created is True
    assert user.email is None
    assert user.display_name == "Octo Builder"


def test_unverified_github_email_never_merges_with_an_existing_account(db, make_user):
    existing = make_user("Existing Person", email="shared@embercohort.dev")
    user, created = accounts.resolve_github_user(
        db, github_identity(email="shared@embercohort.dev", email_verified=False)
    )
    db.commit()

    assert created is True
    assert user.id != existing.id


def test_verified_github_email_links_to_the_existing_account(db, make_user):
    existing = make_user("Existing Person", email="verified@embercohort.dev")
    user, created = accounts.resolve_github_user(
        db, github_identity(email="verified@embercohort.dev", email_verified=True)
    )
    db.commit()

    assert created is False
    assert user.id == existing.id
    assert len(db.scalars(select(User)).all()) == 1


def test_github_token_is_not_stored_by_default(db):
    user, _ = accounts.resolve_github_user(db, github_identity())
    db.commit()
    link = db.scalar(select(OAuthAccount).where(OAuthAccount.user_id == user.id))
    assert link.access_token_encrypted is None


def test_github_token_when_stored_is_encrypted_and_never_serialised(db, monkeypatch):
    monkeypatch.setattr(settings, "store_github_tokens", True)
    user, _ = accounts.resolve_github_user(db, github_identity())
    db.commit()
    link = db.scalar(select(OAuthAccount).where(OAuthAccount.user_id == user.id))
    assert link.access_token_encrypted is not None
    assert "gho_secret_value" not in link.access_token_encrypted

    from app.schemas.common import CurrentUserOut

    assert "gho_secret_value" not in CurrentUserOut.model_validate(user).model_dump_json()
    monkeypatch.setattr(settings, "store_github_tokens", False)


def test_oauth_state_is_single_use(db):
    from app.auth import github

    url = github.build_authorization_url(db)
    db.commit()
    state = url.split("state=")[1].split("&")[0]

    consumed = github.consume_state(db, state)
    db.commit()
    assert consumed.consumed_at is not None

    with pytest.raises(AuthenticationError):
        github.consume_state(db, state)


def test_oauth_state_rejects_unknown_values(db):
    from app.auth import github

    with pytest.raises(AuthenticationError):
        github.consume_state(db, "not-a-real-state")
    with pytest.raises(AuthenticationError):
        github.consume_state(db, None)


def test_registration_creates_identity_only_users(db):
    # Roles live on the cohort membership now -- registration never assigns one.
    admin = accounts.register_with_password(
        db, email="boss@embercohort.dev", password="correct-horse-9", display_name="The Boss"
    )
    db.commit()
    assert not hasattr(admin, "role")
    assert (
        db.scalar(select(CohortMembership).where(CohortMembership.user_id == admin.id))
        is None
    )


def test_role_cannot_be_set_through_the_signup_payload(client, db):
    response = client.post(
        "/api/auth/signup",
        json={
            "email": "sneaky@embercohort.dev",
            "password": "correct-horse-9",
            "display_name": "Sneaky Person",
            "role": "admin",
            "is_admin": True,
        },
    )
    assert response.status_code == 201
    # No role is exposed on the account, and none was created.
    assert "role" not in response.json()["user"]
    user = db.scalar(select(User).where(User.email == "sneaky@embercohort.dev"))
    assert (
        db.scalar(select(CohortMembership).where(CohortMembership.user_id == user.id))
        is None
    )


def test_role_cannot_be_changed_through_the_profile_endpoint(client, make_user, sign_in, db):
    user = make_user("Not An Admin", email="notadmin@embercohort.dev")
    sign_in(user)
    client.patch("/api/profile", json={"bio": "hi", "role": "admin"})
    db.refresh(user.membership)
    assert user.membership.role is UserRole.MEMBER


def test_changing_password_invalidates_other_sessions(client, make_user, fresh_client):
    user = make_user("Rotator", email="rotate@embercohort.dev")
    other = fresh_client()
    other.post(
        "/api/auth/login", json={"email": user.email, "password": "correct-horse-9-battery"}
    )
    client.post(
        "/api/auth/login", json={"email": user.email, "password": "correct-horse-9-battery"}
    )

    response = client.post(
        "/api/auth/password",
        json={"current_password": "correct-horse-9-battery", "new_password": "brand-new-pass-1"},
    )
    assert response.status_code == 200
    assert other.get("/api/auth/me").status_code == 401

    fresh = fresh_client()
    assert (
        fresh.post(
            "/api/auth/login", json={"email": user.email, "password": "brand-new-pass-1"}
        ).status_code
        == 200
    )


def test_setting_a_password_twice_conflicts(db, make_user):
    user = make_user("Has Password", email="haspw@embercohort.dev")
    with pytest.raises(ConflictError):
        accounts.set_initial_password(db, user=user, new_password="another-pass-1")


def test_session_cookie_is_http_only(client, make_user):
    user = make_user("Cookie Check", email="cookie@embercohort.dev")
    response = client.post(
        "/api/auth/login", json={"email": user.email, "password": "correct-horse-9-battery"}
    )
    set_cookie = response.headers.get("set-cookie", "")
    assert "httponly" in set_cookie.lower()
    assert "samesite=lax" in set_cookie.lower()


def test_resolve_session_rejects_revoked_sessions(db, make_user):
    user = make_user("Revoked", email="revoked@embercohort.dev")
    session_row, raw = sessions.create_session(db, user)
    db.commit()
    assert sessions.resolve_session(db, raw) is not None

    sessions.revoke_session(db, session_row.id)
    db.commit()
    assert sessions.resolve_session(db, raw) is None
