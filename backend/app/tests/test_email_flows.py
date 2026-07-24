"""Email verification and password reset."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.core.enums import EmailTokenPurpose
from app.core.errors import ValidationError
from app.models.user import EmailToken, PasswordCredential, UserSession
from app.services import accounts, credentials


def _token_from(email_text: str) -> str:
    match = re.search(r"token=([A-Za-z0-9_-]+)", email_text)
    assert match, f"no token in email:\n{email_text}"
    return match.group(1)


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


def test_password_reset_end_to_end(client, make_user, captured_email, db, fresh_client):
    user = make_user("Reset Me", email="resetme@embercohort.dev")

    # 1. Request a reset -> neutral response, one email, a stored (hashed) token.
    response = client.post("/api/auth/password/forgot", json={"email": user.email})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert len(captured_email) == 1

    stored = db.scalars(
        select(EmailToken).where(
            EmailToken.user_id == user.id,
            EmailToken.purpose == EmailTokenPurpose.RESET_PASSWORD,
        )
    ).all()
    assert len(stored) == 1
    raw = _token_from(captured_email[0].text_body)
    assert stored[0].token_hash != raw  # only the hash is stored

    # 2. Use the token -> password changes.
    reset = client.post(
        "/api/auth/password/reset", json={"token": raw, "new_password": "brand-new-pass-1"}
    )
    assert reset.status_code == 200

    # 3. The new password works; the old one does not.
    other = fresh_client()
    assert (
        other.post(
            "/api/auth/login", json={"email": user.email, "password": "brand-new-pass-1"}
        ).status_code
        == 200
    )
    assert (
        other.post(
            "/api/auth/login",
            json={"email": user.email, "password": "correct-horse-9-battery"},
        ).status_code
        == 401
    )


def test_reset_link_is_single_use(client, make_user, captured_email):
    user = make_user("Single Use", email="single@embercohort.dev")
    client.post("/api/auth/password/forgot", json={"email": user.email})
    raw = _token_from(captured_email[0].text_body)

    first = client.post(
        "/api/auth/password/reset", json={"token": raw, "new_password": "first-new-pass-1"}
    )
    assert first.status_code == 200

    second = client.post(
        "/api/auth/password/reset", json={"token": raw, "new_password": "second-new-pass-1"}
    )
    assert second.status_code == 422
    assert second.json()["error"]["code"] == "TOKEN_INVALID"


def test_reset_revokes_existing_sessions(client, make_user, captured_email, db, sign_in):
    user = make_user("Revoke On Reset", email="revoke@embercohort.dev")
    sign_in(user)  # establishes a live session on `client`
    assert client.get("/api/auth/me").status_code == 200

    client.post("/api/auth/password/forgot", json={"email": user.email})
    raw = _token_from(captured_email[0].text_body)
    client.post(
        "/api/auth/password/reset", json={"token": raw, "new_password": "rotated-pass-9-x"}
    )

    # Every prior session for this user is now revoked.
    live = db.scalars(
        select(UserSession).where(
            UserSession.user_id == user.id, UserSession.revoked_at.is_(None)
        )
    ).all()
    assert live == []


def test_forgot_password_is_neutral_for_unknown_addresses(client, captured_email):
    response = client.post(
        "/api/auth/password/forgot", json={"email": "nobody@embercohort.dev"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    # No account, so no reset email is sent.
    assert captured_email == []


def test_forgot_password_for_a_github_only_account_sends_guidance_not_a_link(
    db, make_user, captured_email
):
    # A user with an email but no password credential (as GitHub sign-in yields).
    from app.services.accounts import GitHubIdentity

    identity = GitHubIdentity(
        provider_account_id="9001",
        username="ghuser",
        display_name="GH User",
        avatar_url=None,
        email="ghonly@embercohort.dev",
        email_verified=True,
    )
    user, _ = accounts.resolve_github_user(db, identity)
    db.commit()
    assert (
        db.scalar(select(PasswordCredential).where(PasswordCredential.user_id == user.id))
        is None
    )

    issue = credentials.request_password_reset(db, email="ghonly@embercohort.dev")
    db.commit()
    assert issue is not None
    assert len(captured_email) == 1
    assert "already have an Ember account" in captured_email[0].subject
    # No reset token was minted for a passwordless account.
    assert (
        db.scalars(
            select(EmailToken).where(
                EmailToken.user_id == user.id,
                EmailToken.purpose == EmailTokenPurpose.RESET_PASSWORD,
            )
        ).all()
        == []
    )


def test_reset_rejects_a_weak_new_password(client, make_user, captured_email):
    user = make_user("Weak Reset", email="weakreset@embercohort.dev")
    client.post("/api/auth/password/forgot", json={"email": user.email})
    raw = _token_from(captured_email[0].text_body)
    response = client.post(
        "/api/auth/password/reset", json={"token": raw, "new_password": "short"}
    )
    assert response.status_code == 422


def test_reset_is_rate_limited_per_ip(client, make_user, monkeypatch, captured_email):
    monkeypatch.setattr(settings, "password_reset_max_requests_per_ip", 3)
    make_user("Reset Spam", email="resetspam@embercohort.dev")
    codes = [
        client.post(
            "/api/auth/password/forgot", json={"email": "resetspam@embercohort.dev"}
        ).status_code
        for _ in range(5)
    ]
    assert 429 in codes


# ---------------------------------------------------------------------------
# Email verification (opt-in)
# ---------------------------------------------------------------------------


@pytest.fixture
def verification_required(monkeypatch):
    monkeypatch.setattr(settings, "require_email_verification", True)
    return True


def test_signup_with_verification_creates_an_unconfirmed_account(
    client, verification_required, captured_email, db
):
    response = client.post(
        "/api/auth/signup",
        json={
            "email": "newperson@embercohort.dev",
            "password": "correct-horse-9",
            "display_name": "New Person",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["verification_required"] is True
    assert body["authenticated"] is False
    assert body["user"] is None
    # No session cookie was set.
    assert settings.session_cookie_name not in response.cookies

    user = accounts.find_by_email(db, "newperson@embercohort.dev")
    assert user is not None
    assert user.email_verified is False
    assert len(captured_email) == 1
    assert "Confirm your" in captured_email[0].subject


def test_unverified_account_is_blocked_until_confirmed(
    client, verification_required, captured_email, db
):
    client.post(
        "/api/auth/signup",
        json={
            "email": "blockme@embercohort.dev",
            "password": "correct-horse-9",
            "display_name": "Block Me",
        },
    )
    # Sign in succeeds (session issued), but the app is gated.
    login = client.post(
        "/api/auth/login",
        json={"email": "blockme@embercohort.dev", "password": "correct-horse-9"},
    )
    assert login.status_code == 200

    blocked = client.get("/api/channels")
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "EMAIL_NOT_VERIFIED"

    # Auth endpoints stay reachable so they can verify or sign out.
    assert client.get("/api/auth/me").status_code == 200

    # Confirm with the emailed token.
    raw = _token_from(captured_email[0].text_body)
    confirm = client.post("/api/auth/email/verify", json={"token": raw})
    assert confirm.status_code == 200

    db.expire_all()
    # Email gate lifted: the only thing left is choosing a cohort.
    after = client.get("/api/channels")
    assert after.status_code == 409
    assert after.json()["error"]["code"] == "NO_ACTIVE_COHORT"


def test_verification_signup_does_not_disclose_existing_addresses(
    client, verification_required, make_user, captured_email
):
    make_user("Existing Person", email="exists@embercohort.dev")
    captured_email.clear()

    new_response = client.post(
        "/api/auth/signup",
        json={
            "email": "fresh@embercohort.dev",
            "password": "correct-horse-9",
            "display_name": "Fresh Person",
        },
    )
    existing_response = client.post(
        "/api/auth/signup",
        json={
            "email": "exists@embercohort.dev",
            "password": "correct-horse-9",
            "display_name": "Impersonator",
        },
    )

    # Both responses are indistinguishable.
    assert new_response.status_code == existing_response.status_code == 201
    assert new_response.json() == existing_response.json()
    # The real owner is told someone tried to sign up as them.
    assert any(
        e.to == "exists@embercohort.dev" and "already have" in e.subject
        for e in captured_email
    )


def test_verification_token_is_single_use(db, make_user, captured_email):
    user = make_user("Verify Once", email="verifyonce@embercohort.dev")
    credentials.send_verification_email(db, user=user)
    db.commit()
    raw = _token_from(captured_email[0].text_body)

    credentials.verify_email(db, raw_token=raw)
    db.commit()
    with pytest.raises(ValidationError):
        credentials.verify_email(db, raw_token=raw)


def test_changing_email_invalidates_an_outstanding_verification_token(
    db, make_user, captured_email
):
    user = make_user("Mover", email="mover@embercohort.dev")
    credentials.send_verification_email(db, user=user)
    db.commit()
    raw = _token_from(captured_email[0].text_body)

    # The address the token was issued for changes before it is used.
    user.user.email = "moved@embercohort.dev"
    db.commit()

    with pytest.raises(ValidationError):
        credentials.verify_email(db, raw_token=raw)


def test_verification_not_required_by_default(client, captured_email, db):
    """With the default policy an account is usable immediately."""

    assert settings.require_email_verification is False
    response = client.post(
        "/api/auth/signup",
        json={
            "email": "immediate@embercohort.dev",
            "password": "correct-horse-9",
            "display_name": "Immediate Person",
        },
    )
    assert response.status_code == 201
    assert response.json()["authenticated"] is True
    # Signed straight in, not email-gated -- only the cohort picker remains.
    immediate = client.get("/api/channels")
    assert immediate.status_code == 409
    assert immediate.json()["error"]["code"] == "NO_ACTIVE_COHORT"


# ---------------------------------------------------------------------------
# Server-rendered pages
# ---------------------------------------------------------------------------


def test_signin_page_links_to_password_reset(client):
    page = client.get("/signin")
    assert 'href="/forgot-password"' in page.text
    assert "Forgot your password?" in page.text


def test_forgot_password_page_and_neutral_confirmation(client, make_user, captured_email):
    make_user("Web Reset", email="webreset@embercohort.dev")
    assert client.get("/forgot-password").status_code == 200

    known = client.post(
        "/forgot-password", data={"email": "webreset@embercohort.dev"}, follow_redirects=False
    )
    unknown = client.post(
        "/forgot-password", data={"email": "ghost@embercohort.dev"}, follow_redirects=False
    )
    # Same page, same status either way.
    assert known.status_code == unknown.status_code == 200
    assert "Check your email" in known.text
    assert "Check your email" in unknown.text


def test_reset_password_page_and_submit(client, make_user, captured_email, db):
    user = make_user("Web Reset Two", email="webreset2@embercohort.dev")
    client.post("/forgot-password", data={"email": user.email})
    raw = _token_from(captured_email[0].text_body)

    page = client.get(f"/reset-password?token={raw}")
    assert page.status_code == 200
    assert raw in page.text  # token carried in the hidden field

    submit = client.post(
        "/reset-password",
        data={"token": raw, "password": "web-brand-new-1"},
        follow_redirects=False,
    )
    assert submit.status_code == 303
    assert submit.headers["location"] == "/signin?reset=1"


def test_reset_password_page_without_a_token_prompts_for_a_new_link(client):
    page = client.get("/reset-password")
    assert "missing its token" in page.text


def test_verification_pending_page_when_signed_in_unverified(
    client, monkeypatch, captured_email, db
):
    monkeypatch.setattr(settings, "require_email_verification", True)
    client.post(
        "/signup",
        data={
            "display_name": "Web Verify",
            "email": "webverify@embercohort.dev",
            "password": "correct-horse-9",
        },
        follow_redirects=False,
    )
    # A private page redirects an unverified account to the verify prompt.
    client.post(
        "/signin",
        data={"email": "webverify@embercohort.dev", "password": "correct-horse-9"},
        follow_redirects=False,
    )
    redirected = client.get("/channels", follow_redirects=False)
    assert redirected.status_code == 303
    assert redirected.headers["location"] == "/verify-email"

    prompt = client.get("/verify-email")
    assert "Confirm your email" in prompt.text
    assert "webverify@embercohort.dev" in prompt.text


def test_confirm_email_link_signs_in_and_redirects(
    client, monkeypatch, captured_email, db
):
    monkeypatch.setattr(settings, "require_email_verification", True)
    client.post(
        "/signup",
        data={
            "display_name": "Link Verify",
            "email": "linkverify@embercohort.dev",
            "password": "correct-horse-9",
        },
        follow_redirects=False,
    )
    raw = _token_from(captured_email[0].text_body)

    confirm = client.get(f"/verify-email/confirm?token={raw}", follow_redirects=False)
    assert confirm.status_code == 303
    assert confirm.headers["location"] == "/profile/complete"
    assert settings.session_cookie_name in confirm.cookies

    user = accounts.find_by_email(db, "linkverify@embercohort.dev")
    assert user is not None
    db.refresh(user)
    assert user.email_verified is True


def test_confirm_email_with_a_bad_token_shows_an_error(client, monkeypatch):
    monkeypatch.setattr(settings, "require_email_verification", True)
    page = client.get("/verify-email/confirm?token=not-a-real-token")
    assert page.status_code == 422
    assert "didn't work" in page.text
