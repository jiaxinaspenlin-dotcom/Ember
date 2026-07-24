"""Server-side permission checks.

This module is the *only* place authorization decisions are made. The templates
and the JSON API both ask these functions; nothing is ever decided in the
browser.

Authorization is **per cohort**: the ``actor`` is a :class:`CohortMembership`,
which carries the person (``actor.user_id``), their role in this cohort
(``actor.is_admin``) and the cohort itself (``actor.cohort_id``). Resource
ownership is checked against ``actor.user_id``; "admin" means admin *of this
cohort*, never a global role.
"""

from __future__ import annotations

import uuid

from sqlalchemy import exists, select
from sqlalchemy.orm import Session as DbSession

from app.core.enums import DecisionStatus, HelpRequestStatus
from app.core.errors import NotFoundError, PermissionDeniedError
from app.models.action import Decision, HelpRequest, Task
from app.models.channel import (
    Channel,
    ChannelMember,
    DirectConversation,
    DirectConversationMember,
)
from app.models.cohort import CohortMembership
from app.models.message import Message

Actor = CohortMembership


# --------------------------------------------------------------------------
# Roles (per cohort)
# --------------------------------------------------------------------------


def require_admin(actor: Actor) -> None:
    if not actor.is_admin:
        raise PermissionDeniedError("This action requires cohort admin access.")


# --------------------------------------------------------------------------
# Channels
# --------------------------------------------------------------------------


def is_channel_member(db: DbSession, channel_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return bool(
        db.scalar(
            select(
                exists().where(
                    ChannelMember.channel_id == channel_id,
                    ChannelMember.user_id == user_id,
                )
            )
        )
    )


def require_channel_post(db: DbSession, channel: Channel, actor: Actor) -> None:
    if channel.is_archived:
        raise PermissionDeniedError(
            "This channel is archived and is read-only.", code="CHANNEL_ARCHIVED"
        )
    if not is_channel_member(db, channel.id, actor.user_id):
        raise PermissionDeniedError(
            "Join this channel before posting.", code="NOT_A_CHANNEL_MEMBER"
        )


def can_create_channel(actor: Actor) -> bool:
    return actor.user.is_active


def require_channel_create(actor: Actor) -> None:
    if not can_create_channel(actor):
        raise PermissionDeniedError("You cannot create channels.")


def can_manage_channel(channel: Channel, actor: Actor) -> bool:
    """The creator runs their own channel; cohort admins manage any channel."""

    return actor.is_admin or channel.created_by_id == actor.user_id


def require_channel_management(channel: Channel, actor: Actor) -> None:
    if not can_manage_channel(channel, actor):
        raise PermissionDeniedError(
            "Only the channel's creator or a cohort admin can manage it."
        )


def can_join_channel(channel: Channel) -> bool:
    return not channel.is_archived


# --------------------------------------------------------------------------
# Direct conversations
# --------------------------------------------------------------------------


def is_conversation_member(
    db: DbSession, conversation_id: uuid.UUID, user_id: uuid.UUID
) -> bool:
    return bool(
        db.scalar(
            select(
                exists().where(
                    DirectConversationMember.conversation_id == conversation_id,
                    DirectConversationMember.user_id == user_id,
                )
            )
        )
    )


def require_conversation_member(
    db: DbSession, conversation: DirectConversation, actor: Actor
) -> None:
    if not is_conversation_member(db, conversation.id, actor.user_id):
        raise NotFoundError("Conversation not found.", code="CONVERSATION_NOT_FOUND")


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


def can_view_message(db: DbSession, message: Message, actor: Actor) -> bool:
    if message.direct_conversation_id is not None:
        return is_conversation_member(db, message.direct_conversation_id, actor.user_id)
    return True


def require_message_access(db: DbSession, message: Message, actor: Actor) -> None:
    if not can_view_message(db, message, actor):
        raise NotFoundError("Message not found.", code="MESSAGE_NOT_FOUND")


def can_edit_message(message: Message, actor: Actor) -> bool:
    return message.sender_id == actor.user_id and message.deleted_at is None


def require_message_edit(message: Message, actor: Actor) -> None:
    if message.deleted_at is not None:
        raise PermissionDeniedError("This message was deleted.", code="MESSAGE_DELETED")
    if message.sender_id != actor.user_id:
        raise PermissionDeniedError("You can only edit your own messages.")


def can_delete_message(message: Message, actor: Actor) -> bool:
    """Authors remove their own messages; cohort admins remove inappropriate ones."""

    return message.sender_id == actor.user_id or actor.is_admin


def require_message_delete(message: Message, actor: Actor) -> None:
    if not can_delete_message(message, actor):
        raise PermissionDeniedError("You cannot delete this message.")


def can_pin_message(message: Message, actor: Actor) -> bool:
    if message.channel_id is None:
        return False
    channel = message.channel
    if channel is None:
        return actor.is_admin
    return can_manage_channel(channel, actor)


def require_message_pin(message: Message, actor: Actor) -> None:
    if message.channel_id is None:
        raise PermissionDeniedError(
            "Only channel messages can be pinned.", code="PIN_NOT_SUPPORTED"
        )
    if not can_pin_message(message, actor):
        raise PermissionDeniedError(
            "Only the channel's creator or a cohort admin can pin messages."
        )


def can_convert_message(message: Message) -> bool:
    return message.channel_id is not None and message.deleted_at is None


def require_convertible_message(message: Message) -> None:
    if message.direct_conversation_id is not None:
        raise PermissionDeniedError(
            "Direct messages cannot be turned into cohort-wide items.",
            code="MESSAGE_NOT_CONVERTIBLE",
        )
    if message.deleted_at is not None:
        raise PermissionDeniedError("This message was deleted.", code="MESSAGE_DELETED")


# --------------------------------------------------------------------------
# Help requests
# --------------------------------------------------------------------------


def can_claim_help_request(help_request: HelpRequest, actor: Actor) -> bool:
    return (
        help_request.status is HelpRequestStatus.OPEN
        and help_request.requester_id != actor.user_id
    )


def can_unclaim_help_request(help_request: HelpRequest, actor: Actor) -> bool:
    return help_request.status is HelpRequestStatus.CLAIMED and (
        help_request.assigned_helper_id == actor.user_id or actor.is_admin
    )


def can_resolve_help_request(help_request: HelpRequest, actor: Actor) -> bool:
    return help_request.status in {
        HelpRequestStatus.OPEN,
        HelpRequestStatus.CLAIMED,
    } and (
        help_request.requester_id == actor.user_id
        or help_request.assigned_helper_id == actor.user_id
        or actor.is_admin
    )


def can_cancel_help_request(help_request: HelpRequest, actor: Actor) -> bool:
    return help_request.status in {
        HelpRequestStatus.OPEN,
        HelpRequestStatus.CLAIMED,
    } and (help_request.requester_id == actor.user_id or actor.is_admin)


def can_reopen_help_request(help_request: HelpRequest, actor: Actor) -> bool:
    return help_request.status in {
        HelpRequestStatus.RESOLVED,
        HelpRequestStatus.CANCELLED,
    } and (help_request.requester_id == actor.user_id or actor.is_admin)


def can_edit_help_request(help_request: HelpRequest, actor: Actor) -> bool:
    return help_request.requester_id == actor.user_id or actor.is_admin


# --------------------------------------------------------------------------
# Decisions
# --------------------------------------------------------------------------


def can_edit_decision(decision: Decision, actor: Actor) -> bool:
    return decision.author_id == actor.user_id or actor.is_admin


def can_supersede_decision(decision: Decision, actor: Actor) -> bool:
    return decision.status is DecisionStatus.ACTIVE and (
        decision.author_id == actor.user_id or actor.is_admin
    )


def can_reverse_decision(decision: Decision, actor: Actor) -> bool:
    return decision.status is DecisionStatus.ACTIVE and (
        decision.author_id == actor.user_id or actor.is_admin
    )


# --------------------------------------------------------------------------
# Tasks
# --------------------------------------------------------------------------


def can_manage_task(task: Task, actor: Actor) -> bool:
    return task.creator_id == actor.user_id or actor.is_admin


def can_update_task_status(task: Task, actor: Actor) -> bool:
    return task.assignee_id == actor.user_id or can_manage_task(task, actor)


def require_task_management(task: Task, actor: Actor) -> None:
    if not can_manage_task(task, actor):
        raise PermissionDeniedError(
            "Only the task creator or a cohort admin can change this task."
        )


def require_task_status_update(task: Task, actor: Actor) -> None:
    if not can_update_task_status(task, actor):
        raise PermissionDeniedError("You cannot update this task's status.")


# --------------------------------------------------------------------------
# Announcements
# --------------------------------------------------------------------------


def require_announcement_management(actor: Actor) -> None:
    if not actor.is_admin:
        raise PermissionDeniedError("Only cohort admins can manage announcements.")


# --------------------------------------------------------------------------
# Aggregated view-model for templates
# --------------------------------------------------------------------------


def channel_capabilities(db: DbSession, channel: Channel, actor: Actor) -> dict[str, bool]:
    member = is_channel_member(db, channel.id, actor.user_id)
    manages = can_manage_channel(channel, actor)
    return {
        "can_post": member and not channel.is_archived,
        "can_join": not member and not channel.is_archived,
        "can_leave": member,
        "can_manage": manages,
        "can_pin": manages,
        "is_member": member,
    }
