"""Real GitHub OAuth, driven entirely by the FastAPI backend.

Flow (nothing is simulated):

1. ``build_authorization_url`` stores a random ``state`` row in PostgreSQL and
   returns GitHub's authorize URL.
2. GitHub redirects the browser back to the backend callback.
3. ``consume_state`` validates and single-uses the state row.
4. ``exchange_code`` swaps the code for an access token over HTTPS.
5. ``fetch_identity`` reads the GitHub profile (and, if the scope allows, the
   primary verified email).

Only ``read:user`` (plus optional ``user:email``) is requested.  No repository,
organization, or write scope is ever asked for.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from urllib.parse import urlencode

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.core.errors import AuthenticationError, ExternalServiceError, ValidationError
from app.core.security import generate_token
from app.db.base import rows_affected, utcnow
from app.models.user import OAuthState
from app.services.accounts import GitHubIdentity

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"
USER_URL = "https://api.github.com/user"
EMAILS_URL = "https://api.github.com/user/emails"

STATE_TTL = dt.timedelta(minutes=10)
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


def ensure_configured() -> None:
    if not settings.github_oauth_configured:
        raise ValidationError(
            "GitHub sign-in is not configured on this server.",
            code="GITHUB_OAUTH_NOT_CONFIGURED",
            status_code=503,
        )


def build_authorization_url(db: DbSession, *, redirect_to: str | None = None) -> str:
    """Create a single-use state row and return GitHub's authorize URL."""

    ensure_configured()
    state = generate_token(24)
    db.add(
        OAuthState(
            state=state,
            provider="github",
            redirect_to=redirect_to,
            expires_at=utcnow() + STATE_TTL,
        )
    )
    db.flush()
    params = {
        "client_id": settings.github_client_id,
        "redirect_uri": settings.github_oauth_redirect_uri,
        "scope": " ".join(settings.github_scope_list),
        "state": state,
        "allow_signup": "true",
    }
    return f"{AUTHORIZE_URL}?{urlencode(params)}"


def consume_state(db: DbSession, state: str | None) -> OAuthState:
    """Validate the OAuth state exactly once."""

    if not state:
        raise AuthenticationError(
            "Sign-in could not be verified. Please try again.", code="OAUTH_STATE_MISSING"
        )
    row = db.scalar(select(OAuthState).where(OAuthState.state == state))
    now = utcnow()
    if row is None or row.consumed_at is not None or row.expires_at <= now:
        raise AuthenticationError(
            "Sign-in could not be verified. Please try again.", code="OAUTH_STATE_INVALID"
        )
    row.consumed_at = now
    db.flush()
    return row


def purge_expired_states(db: DbSession) -> int:
    result = db.execute(delete(OAuthState).where(OAuthState.expires_at < utcnow()))
    return rows_affected(result)


async def exchange_code(code: str) -> dict[str, Any]:
    ensure_configured()
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            response = await client.post(
                TOKEN_URL,
                headers={"Accept": "application/json"},
                data={
                    "client_id": settings.github_client_id,
                    "client_secret": settings.github_client_secret,
                    "code": code,
                    "redirect_uri": settings.github_oauth_redirect_uri,
                },
            )
    except httpx.HTTPError as exc:
        raise ExternalServiceError(
            "Could not reach GitHub. Please try again.", code="GITHUB_UNREACHABLE"
        ) from exc

    if response.status_code >= 400:
        raise ExternalServiceError(
            "GitHub rejected the sign-in request.", code="GITHUB_TOKEN_EXCHANGE_FAILED"
        )
    payload: dict[str, Any] = response.json()
    if "error" in payload or not payload.get("access_token"):
        raise AuthenticationError(
            "GitHub sign-in was not completed.", code="GITHUB_TOKEN_EXCHANGE_FAILED"
        )
    return payload


async def fetch_identity(access_token: str, *, scopes: str | None = None) -> GitHubIdentity:
    """Read the GitHub identity using the freshly issued access token."""

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            user_response = await client.get(USER_URL, headers=headers)
            if user_response.status_code == 401:
                raise AuthenticationError(
                    "GitHub access was revoked. Please sign in again.",
                    code="GITHUB_ACCESS_REVOKED",
                )
            if user_response.status_code >= 400:
                raise ExternalServiceError(
                    "Could not read your GitHub profile.", code="GITHUB_PROFILE_FAILED"
                )
            profile: dict[str, Any] = user_response.json()

            email: str | None = profile.get("email")
            email_verified = False
            if "user:email" in (scopes or settings.github_oauth_scopes):
                email_response = await client.get(EMAILS_URL, headers=headers)
                if email_response.status_code == 200:
                    primary = _pick_primary_email(email_response.json())
                    if primary is not None:
                        email, email_verified = primary
    except httpx.HTTPError as exc:
        raise ExternalServiceError(
            "Could not reach GitHub. Please try again.", code="GITHUB_UNREACHABLE"
        ) from exc

    account_id = profile.get("id")
    login = profile.get("login")
    if account_id is None or not login:
        raise ExternalServiceError(
            "GitHub returned an unexpected profile.", code="GITHUB_PROFILE_INVALID"
        )

    return GitHubIdentity(
        provider_account_id=str(account_id),
        username=str(login),
        display_name=profile.get("name") or str(login),
        avatar_url=profile.get("avatar_url"),
        email=email,
        email_verified=email_verified,
        access_token=access_token,
        scopes=scopes,
    )


def _pick_primary_email(payload: Any) -> tuple[str, bool] | None:
    """Choose the primary verified email GitHub reports, if any."""

    if not isinstance(payload, list):
        return None
    candidates = [entry for entry in payload if isinstance(entry, dict) and entry.get("email")]
    for entry in candidates:
        if entry.get("primary") and entry.get("verified"):
            return str(entry["email"]), True
    for entry in candidates:
        if entry.get("verified"):
            return str(entry["email"]), True
    if candidates:
        return str(candidates[0]["email"]), bool(candidates[0].get("verified"))
    return None
