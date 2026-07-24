"""Permitted system states.

These are *code-level* enums describing which states the system supports.  They
are not application data: every actual record is created by users and stored in
PostgreSQL.
"""

from __future__ import annotations

from enum import StrEnum


class EmailTokenPurpose(StrEnum):
    VERIFY_EMAIL = "verify_email"
    RESET_PASSWORD = "reset_password"


class UserRole(StrEnum):
    MEMBER = "member"
    ADMIN = "admin"


class WorkingStatus(StrEnum):
    AVAILABLE = "available_to_help"
    BUILDING = "building"
    FOCUS = "in_focus_mode"
    BLOCKED = "blocked"
    AWAY = "away"

    @property
    def label(self) -> str:
        return _WORKING_STATUS_LABELS[self]


_WORKING_STATUS_LABELS: dict[WorkingStatus, str] = {
    WorkingStatus.AVAILABLE: "Available to help",
    WorkingStatus.BUILDING: "Building",
    WorkingStatus.FOCUS: "In focus mode",
    WorkingStatus.BLOCKED: "Blocked",
    WorkingStatus.AWAY: "Away",
}


class MessageType(StrEnum):
    TEXT = "text"
    SYSTEM = "system"
    ANNOUNCEMENT = "announcement"


class ReactionType(StrEnum):
    THUMBS_UP = "thumbs_up"
    EYES = "eyes"
    CHECK = "check"
    CELEBRATION = "celebration"
    HEART = "heart"

    @property
    def emoji(self) -> str:
        return _REACTION_EMOJI[self]

    @property
    def label(self) -> str:
        return _REACTION_LABELS[self]


_REACTION_EMOJI: dict[ReactionType, str] = {
    ReactionType.THUMBS_UP: "\N{THUMBS UP SIGN}",
    ReactionType.EYES: "\N{EYES}",
    ReactionType.CHECK: "\N{WHITE HEAVY CHECK MARK}",
    ReactionType.CELEBRATION: "\N{PARTY POPPER}",
    ReactionType.HEART: "\N{HEAVY BLACK HEART}",
}

_REACTION_LABELS: dict[ReactionType, str] = {
    ReactionType.THUMBS_UP: "Thumbs up",
    ReactionType.EYES: "Eyes",
    ReactionType.CHECK: "Check mark",
    ReactionType.CELEBRATION: "Celebration",
    ReactionType.HEART: "Heart",
}


class MentionType(StrEnum):
    USER = "user"
    CHANNEL = "channel"
    ADMINS = "admins"


class NotificationType(StrEnum):
    MENTION = "mention"
    DIRECT_MESSAGE = "direct_message"
    THREAD_REPLY = "thread_reply"
    TASK_ASSIGNED = "task_assigned"
    TASK_STATUS_CHANGED = "task_status_changed"
    HELP_REQUEST_CLAIMED = "help_request_claimed"
    HELP_REQUEST_ASSIGNED = "help_request_assigned"
    HELP_REQUEST_RESOLVED = "help_request_resolved"
    HELP_REQUEST_REOPENED = "help_request_reopened"
    ANNOUNCEMENT = "announcement"
    DECISION_CHANGED = "decision_changed"
    CHANNEL_INVITE = "channel_invite"
    KUDOS_RECEIVED = "kudos_received"


class Priority(StrEnum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class HelpCategory(StrEnum):
    CODING = "coding"
    DESIGN = "design"
    DEPLOYMENT = "deployment"
    RESEARCH = "research"
    PRODUCT = "product"
    FEEDBACK = "feedback"
    OTHER = "other"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class HelpRequestStatus(StrEnum):
    OPEN = "open"
    CLAIMED = "claimed"
    RESOLVED = "resolved"
    CANCELLED = "cancelled"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class DecisionStatus(StrEnum):
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVERSED = "reversed"

    @property
    def label(self) -> str:
        return self.value.capitalize()


class TaskStatus(StrEnum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    BLOCKED = "blocked"
    DONE = "done"

    @property
    def label(self) -> str:
        return _TASK_STATUS_LABELS[self]


_TASK_STATUS_LABELS: dict[TaskStatus, str] = {
    TaskStatus.TODO: "To do",
    TaskStatus.IN_PROGRESS: "In progress",
    TaskStatus.BLOCKED: "Blocked",
    TaskStatus.DONE: "Done",
}


class AuditAction(StrEnum):
    USER_REGISTERED = "user.registered"
    USER_LOGGED_IN = "user.logged_in"
    USER_LOGGED_OUT = "user.logged_out"
    USER_LOGIN_FAILED = "user.login_failed"
    USER_ROLE_CHANGED = "user.role_changed"
    COHORT_CREATED = "cohort.created"
    COHORT_JOINED = "cohort.joined"
    COHORT_LEFT = "cohort.left"
    COHORT_MEMBER_ROLE_CHANGED = "cohort.member_role_changed"
    COHORT_INVITE_GENERATED = "cohort.invite_generated"
    COHORT_INVITE_REVOKED = "cohort.invite_revoked"
    EMAIL_VERIFICATION_SENT = "user.email_verification_sent"
    EMAIL_VERIFIED = "user.email_verified"
    PASSWORD_RESET_REQUESTED = "user.password_reset_requested"
    PASSWORD_RESET_COMPLETED = "user.password_reset_completed"
    OAUTH_ACCOUNT_LINKED = "oauth.account_linked"
    PROFILE_UPDATED = "profile.updated"
    CHANNEL_CREATED = "channel.created"
    CHANNEL_RENAMED = "channel.renamed"
    CHANNEL_ARCHIVED = "channel.archived"
    CHANNEL_RESTORED = "channel.restored"
    CHANNEL_JOINED = "channel.joined"
    CHANNEL_LEFT = "channel.left"
    CHANNEL_MEMBER_INVITED = "channel.member_invited"
    CHANNEL_MEMBER_REMOVED = "channel.member_removed"
    CHANNEL_INVITE_GENERATED = "channel.invite_generated"
    CHANNEL_INVITE_REVOKED = "channel.invite_revoked"
    MESSAGE_CREATED = "message.created"
    MESSAGE_EDITED = "message.edited"
    MESSAGE_DELETED = "message.deleted"
    MESSAGE_PINNED = "message.pinned"
    MESSAGE_UNPINNED = "message.unpinned"
    HELP_REQUEST_CREATED = "help_request.created"
    HELP_REQUEST_CLAIMED = "help_request.claimed"
    HELP_REQUEST_UNCLAIMED = "help_request.unclaimed"
    HELP_REQUEST_RESOLVED = "help_request.resolved"
    HELP_REQUEST_CANCELLED = "help_request.cancelled"
    HELP_REQUEST_REOPENED = "help_request.reopened"
    DECISION_CREATED = "decision.created"
    DECISION_SUPERSEDED = "decision.superseded"
    DECISION_REVERSED = "decision.reversed"
    TASK_CREATED = "task.created"
    TASK_ASSIGNED = "task.assigned"
    TASK_STATUS_CHANGED = "task.status_changed"
    ANNOUNCEMENT_CREATED = "announcement.created"
    ANNOUNCEMENT_UPDATED = "announcement.updated"
    ANNOUNCEMENT_DELETED = "announcement.deleted"
    KUDOS_GIVEN = "kudos.given"
    CHECK_IN_POSTED = "check_in.posted"
