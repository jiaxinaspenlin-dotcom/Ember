"""Community features: presence, kudos, daily check-ins, and the cohort pulse.

Everything here is cohort-scoped. The pulse feeds the campfire on the home page:
the more a cohort talks, decides, ships and thanks each other, the bigger the
fire.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession

from app.core.enums import (
    AuditAction,
    HelpRequestStatus,
    NotificationType,
    TaskStatus,
)
from app.core.errors import NotFoundError, ValidationError
from app.db.base import utcnow
from app.models.action import Decision, HelpRequest, Task
from app.models.cohort import Cohort, CohortMembership
from app.models.community import CheckIn, Kudos
from app.models.message import Message
from app.models.user import User
from app.services import audit, cohorts, notifications

Actor = CohortMembership

# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------

ONLINE_WINDOW = dt.timedelta(minutes=5)
AWAY_WINDOW = dt.timedelta(minutes=30)


def presence_for(last_active_at: dt.datetime | None, *, now: dt.datetime | None = None) -> str:
    """``online`` / ``away`` / ``offline`` from a user's last activity."""

    if last_active_at is None:
        return "offline"
    now = now or utcnow()
    delta = now - last_active_at
    if delta <= ONLINE_WINDOW:
        return "online"
    if delta <= AWAY_WINDOW:
        return "away"
    return "offline"


def count_online(db: DbSession, *, cohort: Cohort, now: dt.datetime | None = None) -> int:
    """How many members of this cohort are active right now."""

    now = now or utcnow()
    cutoff = now - ONLINE_WINDOW
    return int(
        db.scalar(
            select(func.count())
            .select_from(CohortMembership)
            .join(User, User.id == CohortMembership.user_id)
            .where(
                CohortMembership.cohort_id == cohort.id,
                User.is_active.is_(True),
                User.last_active_at.is_not(None),
                User.last_active_at >= cutoff,
            )
        )
        or 0
    )


# ---------------------------------------------------------------------------
# Kudos (shout-outs)
# ---------------------------------------------------------------------------


def give_kudos(
    db: DbSession,
    *,
    actor: Actor,
    to_user_id: uuid.UUID,
    message: str,
    help_request_id: uuid.UUID | None = None,
    message_id: uuid.UUID | None = None,
) -> Kudos:
    """Thank a fellow cohort member. Both people must be in the actor's cohort."""

    clean = " ".join(message.split())
    if len(clean) < 2:
        raise ValidationError("Write a few words of thanks.", details={"field": "message"})
    if to_user_id == actor.user_id:
        raise ValidationError("You cannot give yourself kudos.", code="KUDOS_SELF")

    recipient = cohorts.get_membership(db, cohort_id=actor.cohort_id, user_id=to_user_id)
    if recipient is None:
        raise NotFoundError("That member is not in this cohort.", code="USER_NOT_FOUND")

    kudos = Kudos(
        cohort_id=actor.cohort_id,
        from_user_id=actor.user_id,
        to_user_id=to_user_id,
        message=clean[:280],
        help_request_id=help_request_id,
        message_id=message_id,
    )
    db.add(kudos)
    db.flush()

    notifications.create_notification(
        db,
        cohort_id=actor.cohort_id,
        recipient_id=to_user_id,
        notification_type=NotificationType.KUDOS_RECEIVED,
        title=f"{actor.user.display_name} gave you kudos",
        body=clean[:200],
        link_path="/kudos",
        actor_id=actor.user_id,
    )
    audit.record(
        db,
        AuditAction.KUDOS_GIVEN,
        actor_id=actor.user_id,
        cohort_id=actor.cohort_id,
        entity_type="kudos",
        entity_id=kudos.id,
        context={"to": str(to_user_id)},
    )
    db.flush()
    return kudos


def list_recent_kudos(db: DbSession, *, cohort: Cohort, limit: int = 30) -> list[Kudos]:
    return list(
        db.scalars(
            select(Kudos)
            .where(Kudos.cohort_id == cohort.id)
            .order_by(Kudos.created_at.desc())
            .limit(limit)
        ).all()
    )


def kudos_received_count(db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(Kudos)
            .where(Kudos.cohort_id == cohort_id, Kudos.to_user_id == user_id)
        )
        or 0
    )


# ---------------------------------------------------------------------------
# Daily check-ins
# ---------------------------------------------------------------------------


def post_check_in(db: DbSession, *, actor: Actor, body: str) -> CheckIn:
    """Post "what I'm working on"; also refreshes the member's current project."""

    clean = body.strip()
    if len(clean) < 2:
        raise ValidationError("Say what you're working on.", details={"field": "body"})

    check_in = CheckIn(cohort_id=actor.cohort_id, user_id=actor.user_id, body=clean[:500])
    db.add(check_in)
    # A check-in is the freshest signal of what someone is doing.
    actor.current_project = clean[:160]
    db.flush()

    audit.record(
        db,
        AuditAction.CHECK_IN_POSTED,
        actor_id=actor.user_id,
        cohort_id=actor.cohort_id,
        entity_type="check_in",
        entity_id=check_in.id,
    )
    db.flush()
    return check_in


def list_recent_check_ins(db: DbSession, *, cohort: Cohort, limit: int = 30) -> list[CheckIn]:
    return list(
        db.scalars(
            select(CheckIn)
            .where(CheckIn.cohort_id == cohort.id)
            .order_by(CheckIn.created_at.desc())
            .limit(limit)
        ).all()
    )


def todays_check_in(
    db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID, now: dt.datetime | None = None
) -> CheckIn | None:
    """The user's most recent check-in today (their local-ish UTC day)."""

    now = now or utcnow()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return db.scalar(
        select(CheckIn)
        .where(
            CheckIn.cohort_id == cohort_id,
            CheckIn.user_id == user_id,
            CheckIn.created_at >= start_of_day,
        )
        .order_by(CheckIn.created_at.desc())
    )


# ---------------------------------------------------------------------------
# Cohort pulse -> the campfire
# ---------------------------------------------------------------------------

# What each kind of activity is "worth" to the fire, over the pulse window.
PULSE_WEIGHTS: dict[str, int] = {
    "messages": 1,
    "check_ins": 2,
    "kudos": 3,
    "tasks_completed": 3,
    "help_resolved": 4,
    "decisions": 5,
    "new_members": 5,
}
# Cumulative score needed to reach each fire level (0 = embers, 5 = roaring).
PULSE_THRESHOLDS: list[int] = [0, 8, 25, 60, 120, 220]


@dataclass(slots=True)
class Pulse:
    window_days: int
    counts: dict[str, int] = field(default_factory=dict)
    score: int = 0
    level: int = 0  # 0..5, drives the campfire size

    @property
    def label(self) -> str:
        return (
            "Cold",
            "Kindling",
            "Catching",
            "Warm",
            "Blazing",
            "Roaring",
        )[self.level]


def compute_pulse(db: DbSession, *, cohort: Cohort, window_days: int = 7) -> Pulse:
    """Score a cohort's recent momentum. Feeds the campfire on the home page."""

    since = utcnow() - dt.timedelta(days=window_days)
    cid = cohort.id

    def _count(model, *conditions) -> int:  # type: ignore[no-untyped-def]
        stmt = (
            select(func.count())
            .select_from(model)
            .where(model.cohort_id == cid, model.created_at >= since)
        )
        for condition in conditions:
            stmt = stmt.where(condition)
        return int(db.scalar(stmt) or 0)

    counts = {
        "messages": _count(Message, Message.deleted_at.is_(None)),
        "check_ins": _count(CheckIn),
        "kudos": _count(Kudos),
        "tasks_completed": int(
            db.scalar(
                select(func.count())
                .select_from(Task)
                .where(
                    Task.cohort_id == cid,
                    Task.status == TaskStatus.DONE,
                    Task.updated_at >= since,
                )
            )
            or 0
        ),
        "help_resolved": int(
            db.scalar(
                select(func.count())
                .select_from(HelpRequest)
                .where(
                    HelpRequest.cohort_id == cid,
                    HelpRequest.status == HelpRequestStatus.RESOLVED,
                    HelpRequest.updated_at >= since,
                )
            )
            or 0
        ),
        "decisions": _count(Decision),
        "new_members": int(
            db.scalar(
                select(func.count())
                .select_from(CohortMembership)
                .where(
                    CohortMembership.cohort_id == cid,
                    CohortMembership.joined_at >= since,
                )
            )
            or 0
        ),
    }

    score = sum(counts[k] * PULSE_WEIGHTS[k] for k in counts)
    level = 0
    for i, threshold in enumerate(PULSE_THRESHOLDS):
        if score >= threshold:
            level = i
    return Pulse(window_days=window_days, counts=counts, score=score, level=level)
