"""Authentication routes: email/password, GitHub OAuth, sessions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.api.dependencies import (
    AuthDep,
    DbDep,
    OptionalAuthDep,
    client_ip,
)
from app.auth import github, sessions
from app.core.config import settings
from app.core.enums import AuditAction
from app.core.errors import EmberError, ValidationError
from app.models.user import User
from app.schemas.auth import (
    AuthStatusResponse,
    ChangePasswordRequest,
    ForgotPasswordRequest,
    LoginRequest,
    NeutralResponse,
    ResetPasswordRequest,
    SessionResponse,
    SetPasswordRequest,
    SignupRequest,
    SignupResponse,
    VerifyEmailRequest,
)
from app.schemas.common import CurrentUserOut, OkResponse
from app.services import accounts, audit, credentials

router = APIRouter(prefix="/api/auth", tags=["auth"])


def _issue_session_cookie(
    response: Response, db: Session, user: User, request: Request
) -> None:
    """Create a server-side session and attach its opaque token as a cookie."""

    _, raw_token = sessions.create_session(
        db,
        user,
        user_agent=request.headers.get("user-agent"),
        ip_address=client_ip(request),
    )
    response.set_cookie(
        settings.session_cookie_name,
        raw_token,
        max_age=settings.session_max_age_days * 24 * 60 * 60,
        **sessions.cookie_kwargs(),  # type: ignore[arg-type]
    )


VERIFICATION_SENT_MESSAGE = (
    "If that address can be registered, we have sent a confirmation link to it. "
    "Check your inbox to finish setting up your account."
)
RESET_SENT_MESSAGE = (
    "If an account exists for that address, we have sent password reset "
    "instructions to it."
)


@router.post(
    "/signup",
    response_model=SignupResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create an account with email and password",
)
def signup(
    payload: SignupRequest, request: Request, response: Response, db: DbDep
) -> SignupResponse:
    outcome = accounts.register_account(
        db,
        email=str(payload.email),
        password=payload.password,
        display_name=payload.display_name,
        ip_address=client_ip(request),
    )

    if not outcome.disclosed:
        # Verification is required: no session, and the same response either way.
        db.commit()
        return SignupResponse(
            verification_required=True, message=VERIFICATION_SENT_MESSAGE
        )

    assert outcome.user is not None
    _issue_session_cookie(response, db, outcome.user, request)
    db.commit()
    return SignupResponse(
        user=CurrentUserOut.model_validate(outcome.user),
        authenticated=True,
        message="Your account is ready.",
    )


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


@router.post(
    "/email/verify",
    response_model=SessionResponse,
    summary="Confirm an email address with a token",
)
def verify_email(
    payload: VerifyEmailRequest, request: Request, response: Response, db: DbDep
) -> SessionResponse:
    user = credentials.verify_email(
        db, raw_token=payload.token, ip_address=client_ip(request)
    )
    # Confirming proves control of the mailbox, so sign them straight in.
    _issue_session_cookie(response, db, user, request)
    db.commit()
    return SessionResponse(user=CurrentUserOut.model_validate(user))


@router.post(
    "/email/resend",
    response_model=NeutralResponse,
    summary="Resend the confirmation email",
)
def resend_verification(request: Request, db: DbDep, auth: AuthDep) -> NeutralResponse:
    if auth.user.email_verified:
        raise ValidationError(
            "This address is already confirmed.", code="EMAIL_ALREADY_VERIFIED"
        )
    issue = credentials.send_verification_email(
        db, user=auth.user, ip_address=client_ip(request)
    )
    db.commit()
    return NeutralResponse(
        message=VERIFICATION_SENT_MESSAGE, delivered=issue.delivery.delivered
    )


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


@router.post(
    "/password/forgot",
    response_model=NeutralResponse,
    summary="Request a password reset link",
)
def forgot_password(
    payload: ForgotPasswordRequest, request: Request, db: DbDep
) -> NeutralResponse:
    """Always responds identically -- an unknown address is not disclosed."""

    issue = credentials.request_password_reset(
        db, email=str(payload.email), ip_address=client_ip(request)
    )
    db.commit()
    return NeutralResponse(
        message=RESET_SENT_MESSAGE,
        delivered=bool(issue and issue.delivery.delivered),
    )


@router.post(
    "/password/reset",
    response_model=OkResponse,
    summary="Set a new password using a reset token",
)
def reset_password(
    payload: ResetPasswordRequest, request: Request, db: DbDep
) -> OkResponse:
    credentials.reset_password(
        db,
        raw_token=payload.token,
        new_password=payload.new_password,
        ip_address=client_ip(request),
    )
    db.commit()
    return OkResponse()


@router.post("/login", response_model=SessionResponse, summary="Sign in with email and password")
def login(
    payload: LoginRequest, request: Request, response: Response, db: DbDep
) -> SessionResponse:
    try:
        user = accounts.authenticate_with_password(
            db,
            email=str(payload.email),
            password=payload.password,
            ip_address=client_ip(request),
        )
    except EmberError:
        # Persist the failed-attempt/audit rows recorded by the service.
        db.commit()
        raise
    _issue_session_cookie(response, db, user, request)
    db.commit()
    return SessionResponse(user=CurrentUserOut.model_validate(user))


@router.post("/logout", response_model=OkResponse, summary="Sign out and revoke the session")
def logout(request: Request, response: Response, db: DbDep, auth: OptionalAuthDep) -> OkResponse:
    if auth is not None:
        sessions.revoke_session(db, auth.session.id)
        audit.record(
            db,
            AuditAction.USER_LOGGED_OUT,
            actor_id=auth.user_id,
            entity_type="session",
            entity_id=auth.session.id,
            ip_address=client_ip(request),
        )
        db.commit()
    response.delete_cookie(
        settings.session_cookie_name,
        **{k: v for k, v in sessions.cookie_kwargs().items() if k in {"path", "domain"}},  # type: ignore[arg-type]
    )
    return OkResponse()


@router.get("/session", response_model=AuthStatusResponse, summary="Current session status")
def session_status(auth: OptionalAuthDep) -> AuthStatusResponse:
    if auth is None:
        return AuthStatusResponse(
            authenticated=False, github_enabled=settings.github_oauth_configured
        )
    return AuthStatusResponse(
        authenticated=True,
        user=CurrentUserOut.model_validate(auth.user),
        github_enabled=settings.github_oauth_configured,
    )


@router.get("/me", response_model=CurrentUserOut, summary="The signed-in user")
def me(auth: AuthDep) -> CurrentUserOut:
    return CurrentUserOut.model_validate(auth.user)


@router.post("/password", response_model=OkResponse, summary="Change your password")
def change_password(payload: ChangePasswordRequest, db: DbDep, auth: AuthDep) -> OkResponse:
    accounts.change_password(
        db,
        user=auth.user,
        current_password=payload.current_password,
        new_password=payload.new_password,
    )
    # Changing a password invalidates every other session.
    sessions.revoke_all_sessions_for_user(db, auth.user_id)
    sessions.revoke_session(db, auth.session.id)
    db.commit()
    return OkResponse()


@router.post(
    "/password/set", response_model=OkResponse, summary="Set a password on a GitHub-only account"
)
def set_password(payload: SetPasswordRequest, db: DbDep, auth: AuthDep) -> OkResponse:
    accounts.set_initial_password(db, user=auth.user, new_password=payload.new_password)
    db.commit()
    return OkResponse()


@router.post("/sessions/revoke-all", response_model=OkResponse, summary="Sign out everywhere")
def revoke_all(db: DbDep, auth: AuthDep) -> OkResponse:
    sessions.revoke_all_sessions_for_user(db, auth.user_id)
    db.commit()
    return OkResponse()


# ---------------------------------------------------------------------------
# GitHub OAuth
# ---------------------------------------------------------------------------


@router.get("/github/start", summary="Begin GitHub OAuth")
def github_start(
    db: DbDep,
    redirect_to: Annotated[str | None, Query(max_length=300)] = None,
) -> RedirectResponse:
    safe_redirect = redirect_to if redirect_to and redirect_to.startswith("/") else None
    url = github.build_authorization_url(db, redirect_to=safe_redirect)
    db.commit()
    return RedirectResponse(url, status_code=status.HTTP_302_FOUND)


@router.get("/github/callback", summary="GitHub OAuth callback")
async def github_callback(
    request: Request,
    db: DbDep,
    code: Annotated[str | None, Query()] = None,
    state: Annotated[str | None, Query()] = None,
    error: Annotated[str | None, Query()] = None,
) -> RedirectResponse:
    """Complete the OAuth handshake and start an Ember session."""

    if error:
        # The user cancelled on GitHub's consent screen.
        return _oauth_failure("github_cancelled")

    try:
        state_row = github.consume_state(db, state)
        db.commit()
    except EmberError:
        db.rollback()
        return _oauth_failure("github_state_invalid")

    if not code:
        return _oauth_failure("github_no_code")

    try:
        token_payload = await github.exchange_code(code)
        identity = await github.fetch_identity(
            str(token_payload["access_token"]), scopes=token_payload.get("scope")
        )
    except EmberError as exc:
        db.rollback()
        return _oauth_failure(exc.code.lower())

    try:
        user, created = accounts.resolve_github_user(
            db, identity, ip_address=client_ip(request)
        )
    except EmberError:
        db.rollback()
        return _oauth_failure("github_link_conflict")

    # Where to land is decided by the web layer: no cohort yet -> the cohort
    # picker; a cohort with an incomplete profile -> /profile/complete.
    del created
    destination = state_row.redirect_to or "/"

    response = RedirectResponse(
        f"{settings.frontend_url.rstrip('/')}{destination}", status_code=status.HTTP_302_FOUND
    )
    _issue_session_cookie(response, db, user, request)
    db.commit()
    return response


def _oauth_failure(reason: str) -> RedirectResponse:
    base = settings.frontend_url.rstrip("/")
    return RedirectResponse(
        f"{base}/signin?error={reason}", status_code=status.HTTP_302_FOUND
    )


@router.get("/github/status", summary="Whether GitHub sign-in is available")
def github_status() -> dict[str, bool]:
    return {"enabled": settings.github_oauth_configured}
