"""Model -> schema conversion.

Keeping this in one place makes it easy to audit what is exposed: note that no
serializer ever emits an email address, password hash, session token, or OAuth
token.
"""

from __future__ import annotations

from app.models.action import Decision, HelpRequest, Task
from app.models.cohort import CohortMembership
from app.models.engagement import Announcement, Notification
from app.models.user import User
from app.schemas.common import UserSummary
from app.schemas.content import (
    AnnouncementOut,
    ChannelListItemOut,
    ChannelOut,
    ConversationOut,
    DecisionOut,
    HelpRequestOut,
    MessageOut,
    NotificationOut,
    ProfileOut,
    ReactionSummaryOut,
    SearchResultOut,
    TaskOut,
)
from app.search.queries import SearchResult
from app.services.channels import ChannelListItem
from app.services.decisions import DecisionView
from app.services.direct_messages import ConversationListItem
from app.services.help_requests import HelpRequestView
from app.services.messages import MessageView, ReactionSummary
from app.services.tasks import TaskView


def user_summary(user: User, *, role: str | None = None) -> UserSummary:
    return UserSummary(
        id=user.id,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        role=role,
    )


def profile_out(membership: CohortMembership) -> ProfileOut:
    return ProfileOut(
        user=user_summary(membership.user, role=membership.role.value),
        bio=membership.bio,
        current_project=membership.current_project,
        project_area=membership.project_area,
        working_status=membership.working_status,
        working_status_label=membership.working_status.label,
        available_to_help=membership.available_to_help,
        skills=membership.skill_names,
    )


def channel_out(channel: object) -> ChannelOut:
    return ChannelOut.model_validate(channel)


def channel_list_item(item: ChannelListItem) -> ChannelListItemOut:
    return ChannelListItemOut(
        channel=ChannelOut.model_validate(item.channel),
        is_member=item.is_member,
        unread_count=item.unread_count,
        last_message_at=item.last_message_at,
    )


def reaction_summary(summary: ReactionSummary) -> ReactionSummaryOut:
    return ReactionSummaryOut(
        reaction_type=summary.reaction_type,
        emoji=summary.reaction_type.emoji,
        label=summary.reaction_type.label,
        count=summary.count,
        reacted=summary.reacted,
        participants=summary.participants,
    )


def message_out(view: MessageView) -> MessageOut:
    message = view.message
    body = "This message was deleted." if message.deleted_at else message.body
    return MessageOut(
        id=message.id,
        seq=message.seq,
        sender=user_summary(message.sender),
        channel_id=message.channel_id,
        direct_conversation_id=message.direct_conversation_id,
        parent_message_id=message.parent_message_id,
        body=body,
        message_type=message.message_type.value,
        created_at=message.created_at,
        edited_at=message.edited_at,
        deleted_at=message.deleted_at,
        is_pinned=message.is_pinned,
        reply_count=message.reply_count,
        last_reply_at=message.last_reply_at,
        reactions=[reaction_summary(r) for r in view.reactions],
        can_edit=view.can_edit,
        can_delete=view.can_delete,
        can_pin=view.can_pin,
        can_convert=view.can_convert,
    )


def conversation_out(item: ConversationListItem) -> ConversationOut:
    last = item.last_message
    excerpt = None
    if last is not None:
        excerpt = "Message deleted" if last.deleted_at else last.body[:120]
    return ConversationOut(
        id=item.conversation.id,
        other_member=user_summary(item.other_member),
        unread_count=item.unread_count,
        last_message_at=item.conversation.last_message_at,
        last_message_excerpt=excerpt,
    )


def notification_out(notification: Notification) -> NotificationOut:
    return NotificationOut(
        id=notification.id,
        notification_type=notification.notification_type.value,
        title=notification.title,
        body=notification.body,
        link_path=notification.link_path,
        read_at=notification.read_at,
        created_at=notification.created_at,
        actor=user_summary(notification.actor) if notification.actor else None,
    )


def announcement_out(announcement: Announcement) -> AnnouncementOut:
    return AnnouncementOut(
        id=announcement.id,
        title=announcement.title,
        body=announcement.body,
        priority=announcement.priority,
        published_at=announcement.published_at,
        expires_at=announcement.expires_at,
        is_pinned=announcement.is_pinned,
        author=user_summary(announcement.author),
    )


def help_request_out(view: HelpRequestView) -> HelpRequestOut:
    item: HelpRequest = view.help_request
    return HelpRequestOut(
        id=item.id,
        title=item.title,
        description=item.description,
        category=item.category,
        urgency=item.urgency,
        status=item.status,
        requester=user_summary(item.requester),
        assigned_helper=user_summary(item.assigned_helper) if item.assigned_helper else None,
        source_channel_slug=item.source_channel.slug if item.source_channel else None,
        source_channel_name=item.source_channel.name if item.source_channel else None,
        original_message_id=item.original_message_id,
        created_at=item.created_at,
        claimed_at=item.claimed_at,
        resolved_at=item.resolved_at,
        resolution_note=item.resolution_note,
        can_claim=view.can_claim,
        can_unclaim=view.can_unclaim,
        can_resolve=view.can_resolve,
        can_cancel=view.can_cancel,
        can_reopen=view.can_reopen,
    )


def decision_out(view: DecisionView) -> DecisionOut:
    item: Decision = view.decision
    return DecisionOut(
        id=item.id,
        title=item.title,
        decision_text=item.decision_text,
        context=item.context,
        status=item.status,
        author=user_summary(item.author),
        related_project=item.related_project,
        source_channel_slug=item.source_channel.slug if item.source_channel else None,
        source_channel_name=item.source_channel.name if item.source_channel else None,
        original_message_id=item.original_message_id,
        superseded_by_id=item.superseded_by_id,
        reversal_reason=item.reversal_reason,
        created_at=item.created_at,
        updated_at=item.updated_at,
        can_supersede=view.can_supersede,
        can_reverse=view.can_reverse,
    )


def task_out(view: TaskView) -> TaskOut:
    item: Task = view.task
    return TaskOut(
        id=item.id,
        title=item.title,
        description=item.description,
        status=item.status,
        status_label=item.status.label,
        priority=item.priority,
        creator=user_summary(item.creator),
        assignee=user_summary(item.assignee) if item.assignee else None,
        source_message_id=item.source_message_id,
        source_channel_slug=item.source_channel.slug if item.source_channel else None,
        due_at=item.due_at,
        completed_at=item.completed_at,
        created_at=item.created_at,
        can_manage=view.can_manage,
        can_update_status=view.can_update_status,
    )


def search_result_out(result: SearchResult) -> SearchResultOut:
    return SearchResultOut(
        kind=result.kind,
        id=result.id,
        title=result.title,
        excerpt=result.excerpt,
        source_label=result.source_label,
        link_path=result.link_path,
        author_name=result.author_name,
        created_at=result.created_at,
    )
