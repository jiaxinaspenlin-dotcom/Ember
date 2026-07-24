"""Lightweight home-dashboard summaries, scoped to the active cohort.

Each query is bounded, indexed and filtered by ``cohort_id``. The dashboard
never loads all messages, full DM contents, complete thread history, or another
user's private data -- and never anything from another cohort.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy.orm import Session as DbSession

from app.core.enums import DecisionStatus, HelpRequestStatus
from app.models.action import Decision, HelpRequest, Task
from app.models.cohort import Cohort, CohortMembership
from app.models.engagement import Announcement
from app.models.message import Message
from app.services import (
    announcements,
    channels,
    direct_messages,
    help_requests,
    mentions,
    messages,
    notifications,
    profiles,
    tasks,
)


@dataclass(slots=True)
class DashboardSummary:
    unread_messages: int = 0
    unread_notifications: int = 0
    open_help_requests: int = 0
    my_help_requests: int = 0
    my_open_tasks: int = 0
    active_channels: int = 0
    conversation_count: int = 0
    recent_announcements: list[Announcement] = field(default_factory=list)
    open_help_queue: list[HelpRequest] = field(default_factory=list)
    assigned_help_requests: list[HelpRequest] = field(default_factory=list)
    recent_decisions: list[Decision] = field(default_factory=list)
    my_tasks: list[Task] = field(default_factory=list)
    available_helpers: list[CohortMembership] = field(default_factory=list)
    recent_mentions: list[Message] = field(default_factory=list)
    has_any_channel: bool = False
    member_count: int = 0


def build_summary(db: DbSession, *, ctx) -> DashboardSummary:  # type: ignore[no-untyped-def]
    cohort: Cohort = ctx.cohort
    user = ctx.user
    summary = DashboardSummary()

    summary.unread_messages = messages.total_unread(db, cohort_id=cohort.id, user_id=user.id)
    summary.unread_notifications = notifications.unread_count(
        db, cohort_id=cohort.id, user_id=user.id
    )
    summary.open_help_requests = help_requests.count_by_status(
        db, cohort_id=cohort.id, status=HelpRequestStatus.OPEN
    )
    summary.my_help_requests = help_requests.count_assigned_to(
        db, cohort_id=cohort.id, user_id=user.id
    )
    summary.my_open_tasks = tasks.count_open_for_user(db, cohort_id=cohort.id, user_id=user.id)
    summary.active_channels = channels.channel_count(db, cohort=cohort)
    summary.conversation_count = direct_messages.conversation_count(
        db, cohort_id=cohort.id, user_id=user.id
    )
    summary.has_any_channel = channels.channel_count(db, cohort=cohort, include_archived=True) > 0
    from app.services import cohorts as cohort_service

    summary.member_count = cohort_service.member_count(db, cohort.id)

    summary.recent_announcements = announcements.recent_announcements(db, cohort=cohort, limit=3)

    open_items, _ = help_requests.list_help_requests(
        db,
        cohort=cohort,
        user=user,
        filters=help_requests.HelpFilters(status=HelpRequestStatus.OPEN),
        limit=5,
    )
    summary.open_help_queue = open_items

    assigned, _ = help_requests.list_help_requests(
        db,
        cohort=cohort,
        user=user,
        filters=help_requests.HelpFilters(
            assigned_to_me=True, status=HelpRequestStatus.CLAIMED
        ),
        limit=5,
    )
    summary.assigned_help_requests = assigned

    from app.services import decisions as decisions_service

    summary.recent_decisions, _ = decisions_service.list_decisions(
        db,
        cohort=cohort,
        filters=decisions_service.DecisionFilters(status=DecisionStatus.ACTIVE),
        limit=4,
    )

    summary.my_tasks = tasks.open_tasks_for_user(
        db, cohort_id=cohort.id, user_id=user.id, limit=5
    )
    summary.available_helpers = profiles.list_available_helpers(
        db, cohort=cohort, exclude_user_id=user.id, limit=5
    )
    summary.recent_mentions = mentions.recent_mentions_for_user(
        db, cohort_id=cohort.id, user_id=user.id, limit=5
    )
    return summary
