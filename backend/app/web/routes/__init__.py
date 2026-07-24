"""Registration of all server-rendered page routers."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import Response

from app.web.deps import PageRedirect
from app.web.routes import (
    action_pages,
    auth_pages,
    channel_pages,
    cohort_pages,
    community_pages,
    directory_pages,
    dm_pages,
    home,
)


def register(app: FastAPI) -> None:
    @app.exception_handler(PageRedirect)
    async def handle_page_redirect(request: Request, exc: PageRedirect) -> Response:
        del request
        return exc.response()

    for module in (
        home,
        auth_pages,
        cohort_pages,
        channel_pages,
        dm_pages,
        action_pages,
        directory_pages,
        community_pages,
    ):
        app.include_router(module.router)


__all__ = ["register"]
