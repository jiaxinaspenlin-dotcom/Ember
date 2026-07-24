"""Jinja2 environment, filters and shared page context.

Templates only *render*.  Every permission flag, unread count and status label
they display is computed in Python and passed in.
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from html import escape
from pathlib import Path
from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy.orm import Session as DbSession

from app.core.config import settings
from app.core.enums import (
    DecisionStatus,
    HelpCategory,
    HelpRequestStatus,
    Priority,
    ReactionType,
    TaskStatus,
    WorkingStatus,
)
from app.core.errors import EmberError
from app.db.base import utcnow
from app.models.user import User

TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


def relative_time(value: dt.datetime | None) -> str:
    """Human-friendly relative timestamp, e.g. "4m ago"."""

    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    delta = utcnow() - value
    seconds = int(delta.total_seconds())
    if seconds < 45:
        return "just now"
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    if seconds < 604800:
        return f"{seconds // 86400}d ago"
    return value.strftime("%d %b %Y")


def absolute_time(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.strftime("%d %b %Y, %H:%M UTC")


def iso_time(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.isoformat()


def duration_since(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    seconds = int((utcnow() - value).total_seconds())
    if seconds < 3600:
        return f"{max(1, seconds // 60)} min"
    if seconds < 86400:
        return f"{seconds // 3600} hr"
    return f"{seconds // 86400} days"


_MENTION_RENDER = re.compile(r"(?<![\w@])@([A-Za-z0-9][A-Za-z0-9._-]{0,63})")
_URL_RENDER = re.compile(r"(https?://[^\s<]+)")


def render_body(body: str) -> Markup:
    """Escape a message body, then linkify URLs and highlight mentions.

    Escaping happens *first*, so no user content can inject markup.
    """

    safe = escape(body or "")
    safe = _URL_RENDER.sub(
        lambda m: (
            f'<a href="{m.group(1)}" target="_blank" rel="noopener noreferrer" '
            f'class="message-link">{m.group(1)}</a>'
        ),
        safe,
    )
    safe = _MENTION_RENDER.sub(lambda m: f'<span class="mention">@{m.group(1)}</span>', safe)
    return Markup(safe.replace("\n", "<br>"))


def initials(name: str) -> str:
    parts = [part for part in (name or "").split() if part]
    if not parts:
        return "?"
    if len(parts) == 1:
        return parts[0][:2].upper()
    return (parts[0][0] + parts[-1][0]).upper()


def avatar_hue(identifier: uuid.UUID | str) -> int:
    """Deterministic warm hue for the fallback avatar."""

    digest = sum(ord(char) for char in str(identifier))
    return 15 + (digest % 12) * 5


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    if count == 1:
        return f"{count} {singular}"
    return f"{count} {plural or singular + 's'}"


def presence(value: dt.datetime | None) -> str:
    """``online`` / ``away`` / ``offline`` for a user's ``last_active_at``."""

    from app.services.community import presence_for

    return presence_for(value)


templates.env.filters["presence"] = presence
templates.env.filters["relative_time"] = relative_time
templates.env.filters["absolute_time"] = absolute_time
templates.env.filters["iso_time"] = iso_time
templates.env.filters["duration_since"] = duration_since
templates.env.filters["render_body"] = render_body
templates.env.filters["initials"] = initials
templates.env.filters["avatar_hue"] = avatar_hue
templates.env.filters["pluralize"] = pluralize
templates.env.globals["REACTION_TYPES"] = list(ReactionType)
templates.env.globals["WORKING_STATUSES"] = list(WorkingStatus)
templates.env.globals["HELP_CATEGORIES"] = list(HelpCategory)
templates.env.globals["HELP_STATUSES"] = list(HelpRequestStatus)
templates.env.globals["DECISION_STATUSES"] = list(DecisionStatus)
templates.env.globals["TASK_STATUSES"] = list(TaskStatus)
templates.env.globals["PRIORITIES"] = list(Priority)
templates.env.globals["POLLING_INTERVAL_MS"] = settings.polling_interval_ms
templates.env.globals["APP_NAME"] = "Ember"
templates.env.globals["APP_TAGLINE"] = "Where cohort conversations turn into action."


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def render(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    payload: dict[str, Any] = {
        "request": request,
        "github_enabled": settings.github_oauth_configured,
    }
    if context:
        payload.update(context)
    return templates.TemplateResponse(
        request, template_name, payload, status_code=status_code, headers=headers
    )


def render_fragment(
    request: Request,
    template_name: str,
    context: dict[str, Any] | None = None,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> HTMLResponse:
    return render(
        request, template_name, context, status_code=status_code, headers=headers
    )


def render_error_page(request: Request, error: EmberError) -> HTMLResponse:
    """Render an error as HTML. HTMX requests get an inline banner instead."""

    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(
            request,
            "fragments/error_banner.html",
            {"request": request, "error": error},
            status_code=error.status_code,
        )
    return templates.TemplateResponse(
        request,
        "pages/error.html",
        {"request": request, "error": error, "current_user": _safe_user(request)},
        status_code=error.status_code,
    )


def _safe_user(request: Request) -> User | None:
    auth = getattr(request.state, "auth", None)
    return auth.user if auth is not None else None


def toast(message: str, *, tone: str = "success") -> dict[str, str]:
    return {"message": message, "tone": tone}


# ---------------------------------------------------------------------------
# Shared navigation context
# ---------------------------------------------------------------------------


def navigation_context(db: DbSession, ctx: Any) -> dict[str, Any]:
    """Sidebar data for the active cohort, plus the workspace switcher.

    Bounded queries only -- never the whole database, and always scoped to the
    active cohort.
    """

    from app.services import channels as channel_service
    from app.services import cohorts as cohort_service
    from app.services import direct_messages as dm_service
    from app.services import notifications as notification_service

    cohort = ctx.cohort
    user = ctx.user
    channel_items, _ = channel_service.list_channels(db, cohort=cohort, user=user, limit=50)
    conversations, _ = dm_service.list_conversations(db, cohort=cohort, user=user, limit=20)
    active_visible = len([item for item in channel_items if not item.channel.is_archived])
    archived_count = channel_service.channel_count(
        db, cohort=cohort, include_archived=True
    ) - active_visible
    return {
        "nav_channels": channel_items,
        "nav_conversations": conversations,
        "nav_unread_notifications": notification_service.unread_count(
            db, cohort_id=cohort.id, user_id=user.id
        ),
        "nav_archived_count": max(0, archived_count),
        "nav_cohorts": cohort_service.list_user_cohorts(db, user=user),
    }
