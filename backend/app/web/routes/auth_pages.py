"""Sign-in, sign-up and profile-completion pages (server-rendered forms)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse

from app.api.dependencies import DbDep, OptionalAuthDep, client_ip
from app.auth import sessions
from app.core.config import settings
from app.core.enums import AuditAction, WorkingStatus
from app.core.errors import EmberError
from app.services import accounts, audit, credentials, profiles
from app.web.deps import PageAuth, PageCohort, page_context
from app.web.templating import render

router = APIRouter(tags=["web-auth"])

OAUTH_ERROR_MESSAGES = {
    "github_cancelled": "GitHub sign-in was cancelled.",
    "github_state_invalid": "That sign-in link expired. Please try again.",
    "github_no_code": "GitHub did not return an authorization code.",
    "github_link_conflict": "That GitHub account is already linked to another Ember account.",
    "github_access_revoked": "GitHub access was revoked. Please try again.",
    "github_oauth_not_configured": "GitHub sign-in is not configured on this server.",
    "github_unreachable": "Could not reach GitHub. Please try again.",
    "github_token_exchange_failed": "GitHub could not complete the sign-in.",
    "github_profile_failed": "Could not read your GitHub profile.",
    "github_profile_invalid": "GitHub returned an unexpected profile.",
    "external_service_failed": "Could not reach GitHub. Please try again.",
}


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _issue_cookie(response: Response, db: DbDep, user: object, request: Request) -> None:
    from app.models.user import User

    assert isinstance(user, User)
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


@router.get("/signin", response_class=HTMLResponse, summary="Sign-in page")
def signin_page(
    request: Request,
    auth: OptionalAuthDep,
    next: str | None = None,
    error: str | None = None,
) -> Response:
    if auth is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return render(
        request,
        "pages/signin.html",
        {
            "next": _safe_next(next),
            "error_message": OAUTH_ERROR_MESSAGES.get(error or ""),
            "mode": "signin",
        },
    )


@router.get("/signup", response_class=HTMLResponse, summary="Create-account page")
def signup_page(request: Request, auth: OptionalAuthDep, next: str | None = None) -> Response:
    if auth is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return render(
        request, "pages/signup.html", {"next": _safe_next(next), "mode": "signup"}
    )


@router.post("/signin", response_class=HTMLResponse, summary="Submit sign-in")
def signin_submit(
    request: Request,
    db: DbDep,
    email: Annotated[str, Form(max_length=320)],
    # Capped so an oversized body cannot turn Argon2 verification into a CPU sink.
    password: Annotated[str, Form(max_length=200)],
    next: Annotated[str | None, Form(max_length=300)] = None,
) -> Response:
    destination = _safe_next(next)
    try:
        user = accounts.authenticate_with_password(
            db, email=email, password=password, ip_address=client_ip(request)
        )
    except EmberError as exc:
        db.commit()  # keep the recorded failed attempt
        return render(
            request,
            "pages/signin.html",
            {
                "next": destination,
                "error_message": exc.message,
                "form_email": email,
                "mode": "signin",
            },
            status_code=exc.status_code,
        )
    response = RedirectResponse(destination, status_code=status.HTTP_303_SEE_OTHER)
    _issue_cookie(response, db, user, request)
    db.commit()
    return response


@router.post("/signup", response_class=HTMLResponse, summary="Submit sign-up")
def signup_submit(
    request: Request,
    db: DbDep,
    display_name: Annotated[str, Form(max_length=120)],
    email: Annotated[str, Form(max_length=320)],
    # Capped so an oversized body cannot turn Argon2 verification into a CPU sink.
    password: Annotated[str, Form(max_length=200)],
    next: Annotated[str | None, Form(max_length=300)] = None,
) -> Response:
    try:
        outcome = accounts.register_account(
            db,
            email=email,
            password=password,
            display_name=display_name,
            ip_address=client_ip(request),
        )
    except EmberError as exc:
        db.rollback()
        return render(
            request,
            "pages/signup.html",
            {
                "next": _safe_next(next),
                "error_message": exc.message,
                "form_email": email,
                "form_display_name": display_name,
                "mode": "signup",
            },
            status_code=exc.status_code,
        )

    if not outcome.disclosed:
        # Verification required: never reveal whether the address was new. Show
        # the same "check your inbox" page either way, with no session.
        db.commit()
        return render(
            request,
            "pages/check_email.html",
            {"email": email, "heading": "Confirm your email"},
        )

    assert outcome.user is not None
    # Verification is off here, so the account is usable immediately. A brand-new
    # account has no cohort yet, so land on "/" and let the cohort gate send them
    # to the picker.
    response = RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    _issue_cookie(response, db, outcome.user, request)
    db.commit()
    return response


@router.post("/signout", summary="Sign out")
def signout(request: Request, db: DbDep, auth: OptionalAuthDep) -> Response:
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
    response = RedirectResponse("/signin", status_code=status.HTTP_303_SEE_OTHER)
    cookie_args = {
        key: value
        for key, value in sessions.cookie_kwargs().items()
        if key in {"path", "domain"}
    }
    response.delete_cookie(settings.session_cookie_name, **cookie_args)  # type: ignore[arg-type]
    return response


VERIFICATION_SENT_MESSAGE = (
    "If that address can be registered, we have sent a confirmation link to it."
)
RESET_SENT_MESSAGE = (
    "If an account exists for that address, we have sent password reset "
    "instructions to it."
)


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


@router.get("/verify-email", response_class=HTMLResponse, summary="Awaiting confirmation")
def verify_email_pending(request: Request, db: DbDep, auth: OptionalAuthDep) -> Response:
    # Reachable while signed in but unverified, or signed out (generic prompt).
    if auth is not None and auth.user.email_verified:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    del db
    email = auth.user.email if auth is not None else None
    return render(
        request,
        "pages/verify_email_pending.html",
        {"email": email, "signed_in": auth is not None},
    )


@router.post("/verify-email/resend", response_class=HTMLResponse, summary="Resend confirmation")
def resend_verification(request: Request, db: DbDep, auth: PageAuth) -> Response:
    if auth.user.email_verified:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    message = VERIFICATION_SENT_MESSAGE
    try:
        credentials.send_verification_email(
            db, user=auth.user, ip_address=client_ip(request)
        )
        db.commit()
    except EmberError as exc:
        db.rollback()
        message = exc.message
    return render(
        request,
        "pages/verify_email_pending.html",
        {"email": auth.user.email, "signed_in": True, "notice": message},
    )


@router.get(
    "/verify-email/confirm", response_class=HTMLResponse, summary="Confirm from an email link"
)
def confirm_email(
    request: Request, db: DbDep, token: Annotated[str, Query(max_length=256)] = ""
) -> Response:
    try:
        user = credentials.verify_email(db, raw_token=token, ip_address=client_ip(request))
    except EmberError as exc:
        db.rollback()
        return render(
            request,
            "pages/verify_email_result.html",
            {"ok": False, "message": exc.message},
            status_code=exc.status_code,
        )
    # Confirming proves control of the mailbox: sign them in and send them on.
    response = RedirectResponse("/profile/complete", status_code=status.HTTP_303_SEE_OTHER)
    _issue_cookie(response, db, user, request)
    db.commit()
    return response


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


@router.get("/forgot-password", response_class=HTMLResponse, summary="Request a reset link")
def forgot_password_page(request: Request, auth: OptionalAuthDep) -> Response:
    if auth is not None:
        return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
    return render(request, "pages/forgot_password.html", {})


@router.post("/forgot-password", response_class=HTMLResponse, summary="Submit reset request")
def forgot_password_submit(
    request: Request, db: DbDep, email: Annotated[str, Form(max_length=320)]
) -> Response:
    # The neutral response is the whole point: an unknown address is never
    # disclosed, so this always renders the same confirmation.
    try:
        credentials.request_password_reset(db, email=email, ip_address=client_ip(request))
        db.commit()
    except EmberError:
        db.commit()  # keep any rate-limit rows; still respond neutrally below
    return render(
        request,
        "pages/check_email.html",
        {"email": email, "heading": "Check your email", "message": RESET_SENT_MESSAGE},
    )


@router.get("/reset-password", response_class=HTMLResponse, summary="Choose a new password")
def reset_password_page(
    request: Request, token: Annotated[str, Query(max_length=256)] = ""
) -> Response:
    return render(request, "pages/reset_password.html", {"token": token})


@router.post("/reset-password", response_class=HTMLResponse, summary="Submit a new password")
def reset_password_submit(
    request: Request,
    db: DbDep,
    token: Annotated[str, Form(max_length=256)],
    password: Annotated[str, Form(max_length=200)],
) -> Response:
    try:
        credentials.reset_password(
            db, raw_token=token, new_password=password, ip_address=client_ip(request)
        )
        db.commit()
    except EmberError as exc:
        db.rollback()
        return render(
            request,
            "pages/reset_password.html",
            {"token": token, "error_message": exc.message},
            status_code=exc.status_code,
        )
    return RedirectResponse(
        "/signin?reset=1", status_code=status.HTTP_303_SEE_OTHER
    )


@router.get("/profile/complete", response_class=HTMLResponse, summary="Complete your profile")
def complete_profile_page(request: Request, db: DbDep, ctx: PageCohort) -> Response:
    return render(
        request,
        "pages/profile_complete.html",
        page_context(db, ctx, profile=ctx.member, active_nav="profile"),
    )


@router.post("/profile/complete", response_class=HTMLResponse, summary="Save profile setup")
def complete_profile_submit(
    request: Request,
    db: DbDep,
    ctx: PageCohort,
    display_name: Annotated[str, Form()],
    bio: Annotated[str, Form()] = "",
    skills: Annotated[str, Form()] = "",
    current_project: Annotated[str, Form()] = "",
    project_area: Annotated[str, Form()] = "",
    working_status: Annotated[str, Form()] = WorkingStatus.BUILDING.value,
    available_to_help: Annotated[str | None, Form()] = None,
) -> Response:
    try:
        profiles.update_profile(
            db,
            membership=ctx.member,
            display_name=display_name,
            bio=bio,
            skills=[part.strip() for part in skills.split(",") if part.strip()],
            current_project=current_project,
            project_area=project_area,
            working_status=WorkingStatus(working_status),
            available_to_help=available_to_help is not None,
        )
        ctx.member.profile_completed = True
        db.commit()
    except (EmberError, ValueError) as exc:
        db.rollback()
        message = exc.message if isinstance(exc, EmberError) else "Choose a valid status."
        return render(
            request,
            "pages/profile_complete.html",
            page_context(
                db,
                ctx,
                profile=ctx.member,
                error_message=message,
                active_nav="profile",
            ),
            status_code=422,
        )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)


@router.post("/profile/complete/skip", summary="Skip profile setup for now")
def skip_profile_setup(db: DbDep, ctx: PageCohort) -> Response:
    """Let people into the app without finishing their profile.

    We mark ``profile_completed`` so the home page stops sending them back here;
    they can fill it in any time from Profile.
    """

    if not ctx.member.profile_completed:
        ctx.member.profile_completed = True
        db.commit()
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
