"""Ember application entrypoint.

A single FastAPI application serves both the JSON API (``/api/*``) and the
server-rendered UI.  All business logic lives in ``app/services``; routes are
thin.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.errors import EmberError, ValidationError
from app.core.logging import configure_logging, get_logger
from app.db.session import verify_database

logger = get_logger("ember")
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Verify connectivity at boot. No data is ever created here."""

    configure_logging()
    try:
        verify_database()
        logger.info("Database connection verified (environment=%s)", settings.environment)
    except SQLAlchemyError:
        logger.exception("Database connection failed at startup")
        raise
    yield


def _wants_html(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept or request.headers.get("hx-request") == "true"


def create_app() -> FastAPI:
    configure_logging()
    app = FastAPI(
        title="Ember",
        description="Where cohort conversations turn into action.",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/api/docs" if not settings.is_production else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if not settings.is_production else None,
    )

    _register_security_headers(app)
    _register_error_handlers(app)
    _register_routers(app)

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    return app


# Ember serves its own scripts and styles, so every source is locked to 'self'.
# `unsafe-inline` / `unsafe-eval` are required by the chosen stack: Alpine.js
# evaluates its directives via the Function constructor, and avatar tints plus a
# few HTMX handlers are inline attributes. The valuable clauses here are the
# ones that cannot be relaxed away -- no external script origins, no plugins,
# no framing, no <base> hijacking, and forms that can only post back to us.
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
        "style-src 'self' 'unsafe-inline'",
        # Avatars are user-supplied https URLs (GitHub and similar).
        "img-src 'self' https: data:",
        "font-src 'self'",
        "connect-src 'self'",
        "form-action 'self'",
        "frame-ancestors 'none'",
        "base-uri 'self'",
        "object-src 'none'",
    ]
)


def _register_security_headers(app: FastAPI) -> None:
    """Attach hardening headers to every response."""

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Callable[[Request], Any]) -> Response:
        response: Response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        # Belt and braces with frame-ancestors, for older browsers.
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=(), interest-cohort=()"
        )
        if settings.is_production:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
            )
        return response


def _register_routers(app: FastAPI) -> None:
    from app.api.routes import (
        admin,
        announcements,
        auth,
        channels,
        dashboard,
        decisions,
        direct_messages,
        health,
        help_requests,
        members,
        messages,
        notifications,
        profile,
        reactions,
        search,
        tasks,
        threads,
    )
    from app.web import routes as web_routes

    for module in (
        health,
        auth,
        profile,
        members,
        channels,
        messages,
        threads,
        direct_messages,
        reactions,
        notifications,
        announcements,
        help_requests,
        decisions,
        tasks,
        search,
        dashboard,
        admin,
    ):
        app.include_router(module.router)

    web_routes.register(app)


def _register_error_handlers(app: FastAPI) -> None:
    from app.web.templating import render_error_page

    @app.exception_handler(EmberError)
    async def handle_ember_error(request: Request, exc: EmberError) -> Response:
        if exc.status_code >= 500:
            logger.error("%s: %s", exc.code, exc.message)
        if _wants_html(request):
            return render_error_page(request, exc)
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation(
        request: Request, exc: RequestValidationError
    ) -> Response:
        first = exc.errors()[0] if exc.errors() else {}
        field = ".".join(str(part) for part in first.get("loc", ())[1:]) or "request"
        message = str(first.get("msg", "The submitted values are not valid."))
        error = ValidationError(
            f"{message}" if field == "request" else f"{field}: {message}",
            details={"field": field},
        )
        if _wants_html(request):
            return render_error_page(request, error)
        return JSONResponse(status_code=error.status_code, content=error.to_dict())

    @app.exception_handler(StarletteHTTPException)
    async def handle_http_exception(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        mapped = EmberError(
            str(exc.detail) if exc.detail else "Request failed.",
            code=_http_code_name(exc.status_code),
            status_code=exc.status_code,
        )
        if _wants_html(request):
            return render_error_page(request, mapped)
        return JSONResponse(status_code=mapped.status_code, content=mapped.to_dict())

    @app.exception_handler(SQLAlchemyError)
    async def handle_database_error(request: Request, exc: SQLAlchemyError) -> Response:
        # Never leak SQL or private data; log the type only.
        logger.error("Database failure: %s", type(exc).__name__)
        mapped = EmberError(
            "The database is temporarily unavailable. Please try again.",
            code="DATABASE_UNAVAILABLE",
            status_code=503,
            retryable=True,
        )
        if _wants_html(request):
            return render_error_page(request, mapped)
        return JSONResponse(status_code=mapped.status_code, content=mapped.to_dict())

    @app.exception_handler(Exception)
    async def handle_unexpected(request: Request, exc: Exception) -> Response:
        logger.exception("Unhandled error: %s", type(exc).__name__)
        mapped = EmberError(
            "Something went wrong on our side. Please try again.",
            code="INTERNAL_ERROR",
            status_code=500,
            retryable=True,
        )
        if _wants_html(request):
            return render_error_page(request, mapped)
        return JSONResponse(status_code=mapped.status_code, content=mapped.to_dict())


def _http_code_name(status_code: int) -> str:
    return {
        400: "BAD_REQUEST",
        401: "NOT_AUTHENTICATED",
        403: "PERMISSION_DENIED",
        404: "NOT_FOUND",
        405: "METHOD_NOT_ALLOWED",
        409: "CONFLICT",
        413: "PAYLOAD_TOO_LARGE",
        429: "RATE_LIMITED",
    }.get(status_code, "REQUEST_FAILED")


app = create_app()
