"""SQLAlchemy models.

Importing this package registers every model on ``Base.metadata`` so Alembic
autogeneration and ``create_all`` (tests only) see the full schema.
"""

from __future__ import annotations

from app.db.base import Base
from app.models.action import Decision, HelpRequest, Task
from app.models.channel import (
    Channel,
    ChannelMember,
    DirectConversation,
    DirectConversationMember,
)
from app.models.cohort import Cohort, CohortMembership, MembershipSkill, Skill
from app.models.community import CheckIn, Kudos
from app.models.engagement import Announcement, AuditEvent, Notification
from app.models.message import Mention, Message, Reaction, ReadReceipt
from app.models.user import (
    EmailToken,
    LoginAttempt,
    OAuthAccount,
    OAuthState,
    PasswordCredential,
    User,
    UserSession,
)

__all__ = [
    "Announcement",
    "AuditEvent",
    "Base",
    "Channel",
    "ChannelMember",
    "CheckIn",
    "Cohort",
    "CohortMembership",
    "Decision",
    "DirectConversation",
    "DirectConversationMember",
    "EmailToken",
    "HelpRequest",
    "Kudos",
    "LoginAttempt",
    "MembershipSkill",
    "Mention",
    "Message",
    "Notification",
    "OAuthAccount",
    "OAuthState",
    "PasswordCredential",
    "Reaction",
    "ReadReceipt",
    "Skill",
    "Task",
    "User",
    "UserSession",
]
