"""Response and request schemas for channels, messages, DMs and engagement."""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel, Field

from app.core.enums import (
    DecisionStatus,
    HelpCategory,
    HelpRequestStatus,
    Priority,
    ReactionType,
    TaskStatus,
    WorkingStatus,
)
from app.schemas.common import ORMModel, UserSummary

# ---------------------------------------------------------------------------
# Profiles and members
# ---------------------------------------------------------------------------


class ProfileOut(BaseModel):
    user: UserSummary
    bio: str | None = None
    current_project: str | None = None
    project_area: str | None = None
    working_status: WorkingStatus
    working_status_label: str
    available_to_help: bool
    skills: list[str] = Field(default_factory=list)


class ProfileUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, min_length=2, max_length=120)
    avatar_url: str | None = Field(default=None, max_length=500)
    bio: str | None = Field(default=None, max_length=500)
    current_project: str | None = Field(default=None, max_length=160)
    project_area: str | None = Field(default=None, max_length=80)
    working_status: WorkingStatus | None = None
    available_to_help: bool | None = None
    skills: list[str] | None = Field(default=None, max_length=12)


class WorkingStatusRequest(BaseModel):
    working_status: WorkingStatus


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


class ChannelOut(ORMModel):
    id: uuid.UUID
    slug: str
    name: str
    description: str | None
    topic: str | None
    is_archived: bool
    created_at: dt.datetime


class ChannelListItemOut(BaseModel):
    channel: ChannelOut
    is_member: bool
    unread_count: int
    last_message_at: dt.datetime | None = None


class ChannelInviteRequest(BaseModel):
    user_id: uuid.UUID


class ChannelJoinByCodeRequest(BaseModel):
    invite_code: str = Field(min_length=8, max_length=64)


class ChannelInviteCodeOut(BaseModel):
    invite_code: str
    invite_url: str


class ChannelCreateRequest(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    topic: str | None = Field(default=None, max_length=200)


class ChannelUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=80)
    description: str | None = Field(default=None, max_length=300)
    topic: str | None = Field(default=None, max_length=200)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


class ReactionSummaryOut(BaseModel):
    reaction_type: ReactionType
    emoji: str
    label: str
    count: int
    reacted: bool
    participants: list[str]


class MessageOut(BaseModel):
    id: uuid.UUID
    seq: int
    sender: UserSummary
    channel_id: uuid.UUID | None
    direct_conversation_id: uuid.UUID | None
    parent_message_id: uuid.UUID | None
    body: str
    message_type: str
    created_at: dt.datetime
    edited_at: dt.datetime | None
    deleted_at: dt.datetime | None
    is_pinned: bool
    reply_count: int
    last_reply_at: dt.datetime | None
    reactions: list[ReactionSummaryOut] = Field(default_factory=list)
    can_edit: bool = False
    can_delete: bool = False
    can_pin: bool = False
    can_convert: bool = False


class MessageCreateRequest(BaseModel):
    body: str = Field(min_length=1, max_length=8000)
    parent_message_id: uuid.UUID | None = None


class MessageEditRequest(BaseModel):
    body: str = Field(min_length=1, max_length=8000)


class MessagePage(BaseModel):
    items: list[MessageOut]
    has_older: bool
    oldest_seq: int | None = None
    latest_seq: int = 0


class NewMessagesResponse(BaseModel):
    """The polling response: only messages newer than the client's cursor."""

    items: list[MessageOut]
    latest_seq: int
    count: int


class ThreadOut(BaseModel):
    parent: MessageOut
    replies: list[MessageOut]
    participants: list[UserSummary]
    source_label: str
    reply_count: int


class ReactionRequest(BaseModel):
    reaction_type: ReactionType


class ReadReceiptRequest(BaseModel):
    last_read_message_id: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# Direct messages
# ---------------------------------------------------------------------------


class ConversationOut(BaseModel):
    id: uuid.UUID
    other_member: UserSummary
    unread_count: int
    last_message_at: dt.datetime | None = None
    last_message_excerpt: str | None = None


class ConversationCreateRequest(BaseModel):
    user_id: uuid.UUID


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


class NotificationOut(ORMModel):
    id: uuid.UUID
    notification_type: str
    title: str
    body: str | None
    link_path: str
    read_at: dt.datetime | None
    created_at: dt.datetime
    actor: UserSummary | None = None


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------


class AnnouncementOut(ORMModel):
    id: uuid.UUID
    title: str
    body: str
    priority: Priority
    published_at: dt.datetime
    expires_at: dt.datetime | None
    is_pinned: bool
    author: UserSummary


class AnnouncementCreateRequest(BaseModel):
    title: str = Field(min_length=4, max_length=200)
    body: str = Field(min_length=1)
    priority: Priority = Priority.NORMAL
    expires_at: dt.datetime | None = None
    is_pinned: bool = False


class AnnouncementUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=4, max_length=200)
    body: str | None = None
    priority: Priority | None = None
    expires_at: dt.datetime | None = None
    clear_expiry: bool = False
    is_pinned: bool | None = None


# ---------------------------------------------------------------------------
# Help requests
# ---------------------------------------------------------------------------


class HelpRequestOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str
    category: HelpCategory
    urgency: Priority
    status: HelpRequestStatus
    requester: UserSummary
    assigned_helper: UserSummary | None
    source_channel_slug: str | None
    source_channel_name: str | None
    original_message_id: uuid.UUID | None
    created_at: dt.datetime
    claimed_at: dt.datetime | None
    resolved_at: dt.datetime | None
    resolution_note: str | None
    can_claim: bool = False
    can_unclaim: bool = False
    can_resolve: bool = False
    can_cancel: bool = False
    can_reopen: bool = False


class HelpRequestCreateRequest(BaseModel):
    title: str = Field(min_length=4, max_length=160)
    description: str = Field(min_length=1)
    category: HelpCategory = HelpCategory.OTHER
    urgency: Priority = Priority.NORMAL
    source_message_id: uuid.UUID | None = None


class HelpRequestUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=4, max_length=160)
    description: str | None = None
    category: HelpCategory | None = None
    urgency: Priority | None = None


class HelpResolveRequest(BaseModel):
    resolution_note: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


class DecisionOut(BaseModel):
    id: uuid.UUID
    title: str
    decision_text: str
    context: str | None
    status: DecisionStatus
    author: UserSummary
    related_project: str | None
    source_channel_slug: str | None
    source_channel_name: str | None
    original_message_id: uuid.UUID | None
    superseded_by_id: uuid.UUID | None
    reversal_reason: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    can_supersede: bool = False
    can_reverse: bool = False


class DecisionCreateRequest(BaseModel):
    title: str = Field(min_length=4, max_length=160)
    decision_text: str = Field(min_length=1)
    context: str | None = None
    related_project: str | None = Field(default=None, max_length=160)
    source_message_id: uuid.UUID | None = None


class DecisionUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=4, max_length=160)
    decision_text: str | None = None
    context: str | None = None
    related_project: str | None = Field(default=None, max_length=160)


class DecisionSupersedeRequest(BaseModel):
    superseded_by_id: uuid.UUID


class DecisionReverseRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


class TaskOut(BaseModel):
    id: uuid.UUID
    title: str
    description: str | None
    status: TaskStatus
    status_label: str
    priority: Priority
    creator: UserSummary
    assignee: UserSummary | None
    source_message_id: uuid.UUID | None
    source_channel_slug: str | None
    due_at: dt.datetime | None
    completed_at: dt.datetime | None
    created_at: dt.datetime
    can_manage: bool = False
    can_update_status: bool = False


class TaskCreateRequest(BaseModel):
    title: str = Field(min_length=3, max_length=160)
    description: str | None = None
    assignee_id: uuid.UUID | None = None
    priority: Priority = Priority.NORMAL
    due_at: dt.datetime | None = None
    source_message_id: uuid.UUID | None = None


class TaskUpdateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=160)
    description: str | None = None
    priority: Priority | None = None
    due_at: dt.datetime | None = None
    clear_due_at: bool = False


class TaskAssignRequest(BaseModel):
    assignee_id: uuid.UUID | None = None


class TaskStatusRequest(BaseModel):
    status: TaskStatus


# ---------------------------------------------------------------------------
# Search and dashboard
# ---------------------------------------------------------------------------


class SearchResultOut(BaseModel):
    kind: str
    id: uuid.UUID
    title: str
    excerpt: str
    source_label: str
    link_path: str
    author_name: str | None
    created_at: dt.datetime


class SearchResponseOut(BaseModel):
    results: list[SearchResultOut]
    total: int
    limit: int
    offset: int
    has_more: bool


class DashboardResponse(BaseModel):
    unread_messages: int
    unread_notifications: int
    open_help_requests: int
    my_help_requests: int
    my_open_tasks: int
    active_channels: int
    conversation_count: int
    member_count: int
    recent_announcements: list[AnnouncementOut]
    open_help_queue: list[HelpRequestOut]
    assigned_help_requests: list[HelpRequestOut]
    recent_decisions: list[DecisionOut]
    my_tasks: list[TaskOut]
    available_helpers: list[ProfileOut]
    recent_mentions: list[MessageOut]
