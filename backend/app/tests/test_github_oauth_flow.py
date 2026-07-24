"""End-to-end GitHub OAuth through the real backend routes.

The only thing stubbed is the network: ``httpx`` is pointed at a mock transport
that plays GitHub's role. Everything else -- the authorize redirect, the DB
``state`` row, the code exchange, the identity fetch, account creation and the
session cookie -- is the real code path.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import settings
from app.models.cohort import CohortMembership
from app.models.user import OAuthAccount, OAuthState, User


class FakeGitHub:
    """A mock GitHub that answers the three endpoints Ember calls."""

    def __init__(
        self,
        *,
        account_id: str = "20001",
        login: str = "octobuilder",
        name: str | None = "Octo Builder",
        primary_email: str | None = "octo@embercohort.dev",
        email_verified: bool = True,
        token: str = "gho_test_access_token",
    ) -> None:
        self.account_id = account_id
        self.login = login
        self.name = name
        self.primary_email = primary_email
        self.email_verified = email_verified
        self.token = token
        self.calls: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.calls.append(url)
        if url.startswith("https://github.com/login/oauth/access_token"):
            return httpx.Response(
                200, json={"access_token": self.token, "scope": "read:user,user:email"}
            )
        if url.startswith("https://api.github.com/user/emails"):
            emails = (
                [{"email": self.primary_email, "primary": True, "verified": self.email_verified}]
                if self.primary_email
                else []
            )
            return httpx.Response(200, json=emails)
        if url.startswith("https://api.github.com/user"):
            return httpx.Response(
                200,
                json={"id": int(self.account_id), "login": self.login, "name": self.name,
                      "avatar_url": "https://avatars.example/octo.png", "email": None},
            )
        return httpx.Response(404)  # pragma: no cover


@pytest.fixture
def fake_github(monkeypatch):
    gh = FakeGitHub()

    real_async_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(gh.handler)
        return real_async_client(*args, **kwargs)

    # Patch the symbol the github module actually calls.
    monkeypatch.setattr("app.auth.github.httpx.AsyncClient", _client)
    return gh


def _start_and_get_state(client: TestClient) -> str:
    start = client.get("/api/auth/github/start", follow_redirects=False)
    assert start.status_code == 302
    location = start.headers["location"]
    assert location.startswith("https://github.com/login/oauth/authorize")
    return str(location.split("state=")[1].split("&")[0])


def test_full_github_first_login_creates_account_and_session(client, fake_github, db):
    state = _start_and_get_state(client)
    # The state row is persisted, not held in memory.
    assert db.scalar(select(OAuthState).where(OAuthState.state == state)) is not None

    callback = client.get(
        f"/api/auth/github/callback?code=real-code&state={state}", follow_redirects=False
    )
    assert callback.status_code == 302
    # First login lands on the app root; with no cohort yet the app then sends
    # them to the cohort picker.
    assert callback.headers["location"].rstrip("/").endswith(
        settings.frontend_url.rstrip("/")
    )
    assert settings.session_cookie_name in callback.cookies

    user = db.scalar(select(User).where(User.email == "octo@embercohort.dev"))
    assert user is not None
    assert user.display_name == "Octo Builder"
    assert (
        db.scalar(select(CohortMembership).where(CohortMembership.user_id == user.id))
        is None
    )
    link = db.scalar(select(OAuthAccount).where(OAuthAccount.user_id == user.id))
    assert link is not None
    assert link.provider_account_id == "20001"
    # Token is discarded by default.
    assert link.access_token_encrypted is None

    # The exchange and identity endpoints were really called.
    assert any("access_token" in c for c in fake_github.calls)
    assert any("api.github.com/user" in c for c in fake_github.calls)


def test_returning_github_login_reuses_the_account(client, fake_github, db, fresh_client):
    state = _start_and_get_state(client)
    client.get(f"/api/auth/github/callback?code=c1&state={state}", follow_redirects=False)

    other = fresh_client()
    state2 = other.get("/api/auth/github/start", follow_redirects=False).headers[
        "location"
    ].split("state=")[1].split("&")[0]
    callback = other.get(
        f"/api/auth/github/callback?code=c2&state={state2}", follow_redirects=False
    )
    assert callback.status_code == 302

    assert len(db.scalars(select(User)).all()) == 1
    assert len(db.scalars(select(OAuthAccount)).all()) == 1


def test_callback_rejects_a_replayed_state(client, fake_github, db):
    state = _start_and_get_state(client)
    first = client.get(
        f"/api/auth/github/callback?code=c1&state={state}", follow_redirects=False
    )
    assert first.status_code == 302

    replay = client.get(
        f"/api/auth/github/callback?code=c2&state={state}", follow_redirects=False
    )
    assert replay.status_code == 302
    assert "error=github_state_invalid" in replay.headers["location"]


def test_callback_rejects_an_unknown_state(client, fake_github):
    del fake_github
    response = client.get(
        "/api/auth/github/callback?code=x&state=never-issued", follow_redirects=False
    )
    assert response.status_code == 302
    assert "error=github_state_invalid" in response.headers["location"]


def test_cancelled_authorisation_redirects_with_an_error(client):
    response = client.get(
        "/api/auth/github/callback?error=access_denied", follow_redirects=False
    )
    assert response.status_code == 302
    assert "error=github_cancelled" in response.headers["location"]


def test_verified_github_email_links_to_an_existing_password_account(
    client, fake_github, make_user, db
):
    existing = make_user("Existing Octo", email="octo@embercohort.dev")
    state = _start_and_get_state(client)
    client.get(f"/api/auth/github/callback?code=c&state={state}", follow_redirects=False)

    # No second account: the verified email linked to the one that was there.
    assert len(db.scalars(select(User)).all()) == 1
    link = db.scalar(select(OAuthAccount))
    assert link is not None
    assert link.user_id == existing.id


def test_github_login_stores_and_encrypts_the_token_when_enabled(
    client, monkeypatch, db
):
    monkeypatch.setattr(settings, "store_github_tokens", True)
    gh = FakeGitHub(token="gho_secret_should_be_encrypted")
    real = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(gh.handler)
        return real(*args, **kwargs)

    monkeypatch.setattr("app.auth.github.httpx.AsyncClient", _client)

    state = _start_and_get_state(client)
    client.get(f"/api/auth/github/callback?code=c&state={state}", follow_redirects=False)

    link = db.scalar(select(OAuthAccount))
    assert link is not None
    assert link.access_token_encrypted is not None
    # Ciphertext, not the raw token.
    assert "gho_secret_should_be_encrypted" not in link.access_token_encrypted
    assert "gho_secret_should_be_encrypted" not in json.dumps(
        {"stored": link.access_token_encrypted}
    )
