"""Home dashboard summary route."""

from __future__ import annotations

from fastapi import APIRouter

from app.api.dependencies import CohortDep, DbDep
from app.schemas.content import DashboardResponse
from app.schemas.serializers import (
    announcement_out,
    decision_out,
    help_request_out,
    message_out,
    profile_out,
    task_out,
)
from app.services import dashboard, decisions, help_requests, messages, tasks

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("", response_model=DashboardResponse, summary="Lightweight home summary")
def read_dashboard(db: DbDep, ctx: CohortDep) -> DashboardResponse:
    summary = dashboard.build_summary(db, ctx=ctx)
    return DashboardResponse(
        unread_messages=summary.unread_messages,
        unread_notifications=summary.unread_notifications,
        open_help_requests=summary.open_help_requests,
        my_help_requests=summary.my_help_requests,
        my_open_tasks=summary.my_open_tasks,
        active_channels=summary.active_channels,
        conversation_count=summary.conversation_count,
        member_count=summary.member_count,
        recent_announcements=[
            announcement_out(item) for item in summary.recent_announcements
        ],
        open_help_queue=[
            help_request_out(view)
            for view in help_requests.build_views(summary.open_help_queue, viewer=ctx.member)
        ],
        assigned_help_requests=[
            help_request_out(view)
            for view in help_requests.build_views(
                summary.assigned_help_requests, viewer=ctx.member
            )
        ],
        recent_decisions=[
            decision_out(view)
            for view in decisions.build_views(summary.recent_decisions, viewer=ctx.member)
        ],
        my_tasks=[
            task_out(view) for view in tasks.build_views(summary.my_tasks, viewer=ctx.member)
        ],
        available_helpers=[
            profile_out(membership) for membership in summary.available_helpers
        ],
        recent_mentions=[
            message_out(view)
            for view in messages.build_views(db, summary.recent_mentions, viewer=ctx.member)
        ],
    )
