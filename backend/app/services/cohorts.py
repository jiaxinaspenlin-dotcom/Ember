"""Cohorts (tenants): creation, membership, joining, roles and switching.

A cohort is the isolation boundary. Every other feature is scoped to the
membership returned here. The person who creates a cohort is its admin; joiners
are members. Joining is open (demo) or invite-only (production), controlled by
``COHORT_OPEN_JOIN``.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.enums import AuditAction, UserRole
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationError,
)
from app.core.security import generate_token
from app.models.cohort import Cohort, CohortMembership
from app.models.user import User
from app.services import audit

SLUG_PATTERN = re.compile(r"[^a-z0-9-]+")


@dataclass(slots=True)
class CohortListItem:
    cohort: Cohort
    membership: CohortMembership
    member_count: int


def slugify(name: str) -> str:
    slug = SLUG_PATTERN.sub("-", name.strip().lower()).strip("-")
    return re.sub(r"-{2,}", "-", slug)[:60]


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def get_cohort(db: DbSession, cohort_id: uuid.UUID) -> Cohort:
    cohort = db.get(Cohort, cohort_id)
    if cohort is None:
        raise NotFoundError("Cohort not found.", code="COHORT_NOT_FOUND")
    return cohort


def get_cohort_by_slug(db: DbSession, slug: str) -> Cohort:
    cohort = db.scalar(select(Cohort).where(Cohort.slug == slug))
    if cohort is None:
        raise NotFoundError("Cohort not found.", code="COHORT_NOT_FOUND")
    return cohort


def get_membership(
    db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID
) -> CohortMembership | None:
    return db.scalar(
        select(CohortMembership)
        .options(selectinload(CohortMembership.skills))
        .where(
            CohortMembership.cohort_id == cohort_id,
            CohortMembership.user_id == user_id,
        )
    )


def require_membership(db: DbSession, *, cohort: Cohort, user: User) -> CohortMembership:
    """Return the user's membership in a cohort, or 404 if they are not in it.

    A non-member must not even learn the cohort exists via its content, so this
    raises ``NotFoundError`` rather than a permission error.
    """

    membership = get_membership(db, cohort_id=cohort.id, user_id=user.id)
    if membership is None:
        raise NotFoundError("Cohort not found.", code="COHORT_NOT_FOUND")
    return membership


def member_count(db: DbSession, cohort_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(CohortMembership)
            .where(CohortMembership.cohort_id == cohort_id)
        )
        or 0
    )


def is_member(db: DbSession, *, cohort_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    return get_membership(db, cohort_id=cohort_id, user_id=user_id) is not None


# ---------------------------------------------------------------------------
# The user's cohorts
# ---------------------------------------------------------------------------


def list_user_cohorts(db: DbSession, *, user: User) -> list[CohortListItem]:
    rows = db.execute(
        select(CohortMembership, Cohort)
        .join(Cohort, Cohort.id == CohortMembership.cohort_id)
        .where(CohortMembership.user_id == user.id)
        .order_by(Cohort.name.asc())
    ).all()
    return [
        CohortListItem(
            cohort=cohort, membership=membership, member_count=member_count(db, cohort.id)
        )
        for membership, cohort in rows
    ]


def user_cohort_count(db: DbSession, *, user_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(CohortMembership)
            .where(CohortMembership.user_id == user_id)
        )
        or 0
    )


def list_joinable(db: DbSession, *, user: User, limit: int = 100) -> list[CohortListItem]:
    """Cohorts the user is not yet in.

    Only surfaced when ``COHORT_OPEN_JOIN`` is on (the demo): otherwise cohorts
    are not discoverable and you need an invite link.
    """

    if not settings.cohort_open_join:
        return []
    joined = select(CohortMembership.cohort_id).where(CohortMembership.user_id == user.id)
    cohorts = db.scalars(
        select(Cohort).where(Cohort.id.not_in(joined)).order_by(Cohort.name.asc()).limit(limit)
    ).all()
    return [
        CohortListItem(cohort=cohort, membership=None, member_count=member_count(db, cohort.id))  # type: ignore[arg-type]
        for cohort in cohorts
    ]


# ---------------------------------------------------------------------------
# Creation and joining
# ---------------------------------------------------------------------------


def _add_membership(
    db: DbSession, *, cohort: Cohort, user: User, role: UserRole
) -> CohortMembership:
    membership = CohortMembership(cohort_id=cohort.id, user_id=user.id, role=role)
    db.add(membership)
    db.flush()
    return membership


def create_cohort(
    db: DbSession, *, creator: User, name: str, description: str | None = None
) -> CohortMembership:
    """Create a cohort. The creator becomes its admin. Returns their membership."""

    # Cap how many cohorts one account can spin up, so open-join cannot be used
    # to flood the picker. 0 disables the limit.
    cap = settings.max_cohorts_created_per_user
    if cap > 0:
        created = int(
            db.scalar(
                select(func.count())
                .select_from(Cohort)
                .where(Cohort.created_by_id == creator.id)
            )
            or 0
        )
        if created >= cap:
            raise ConflictError(
                f"You can create at most {cap} cohorts.",
                code="COHORT_CREATE_LIMIT",
            )

    clean_name = " ".join(name.split())
    if len(clean_name) < 2:
        raise ValidationError(
            "Cohort name must be at least 2 characters.", details={"field": "name"}
        )
    if len(clean_name) > 80:
        raise ValidationError(
            "Cohort name must be at most 80 characters.", details={"field": "name"}
        )
    base_slug = slugify(clean_name)
    if not base_slug:
        raise ValidationError(
            "Cohort name must contain letters or numbers.", details={"field": "name"}
        )
    slug = _unique_slug(db, base_slug)

    cohort = Cohort(
        slug=slug,
        name=clean_name,
        description=(description or "").strip()[:300] or None,
        created_by_id=creator.id,
    )
    db.add(cohort)
    db.flush()

    membership = _add_membership(db, cohort=cohort, user=creator, role=UserRole.ADMIN)
    audit.record(
        db,
        AuditAction.COHORT_CREATED,
        actor_id=creator.id,
        cohort_id=cohort.id,
        entity_type="cohort",
        entity_id=cohort.id,
        context={"slug": slug},
    )
    db.flush()
    return membership


def _unique_slug(db: DbSession, base: str) -> str:
    """Cohort slugs are global (used for switching), so disambiguate collisions."""

    slug = base
    suffix = 2
    while db.scalar(select(Cohort.id).where(Cohort.slug == slug)) is not None:
        slug = f"{base[:56]}-{suffix}"
        suffix += 1
    return slug


def join_cohort(db: DbSession, *, user: User, cohort: Cohort) -> CohortMembership:
    """Join a cohort directly (open-join, or after following an invite)."""

    existing = get_membership(db, cohort_id=cohort.id, user_id=user.id)
    if existing is not None:
        return existing
    membership = _add_membership(db, cohort=cohort, user=user, role=UserRole.MEMBER)
    audit.record(
        db,
        AuditAction.COHORT_JOINED,
        actor_id=user.id,
        cohort_id=cohort.id,
        entity_type="cohort",
        entity_id=cohort.id,
    )
    try:
        db.flush()
    except IntegrityError:  # pragma: no cover - concurrent join
        db.rollback()
        existing = get_membership(db, cohort_id=cohort.id, user_id=user.id)
        if existing is None:
            raise
        return existing
    return membership


def open_join(db: DbSession, *, user: User, cohort: Cohort) -> CohortMembership:
    """Join a discoverable cohort. Only allowed while open-join is enabled."""

    if not settings.cohort_open_join:
        raise PermissionDeniedError(
            "This cohort can only be joined with an invite link.",
            code="COHORT_INVITE_REQUIRED",
        )
    return join_cohort(db, user=user, cohort=cohort)


def join_by_invite(db: DbSession, *, user: User, invite_code: str) -> CohortMembership:
    if not invite_code:
        raise NotFoundError("This invite link is invalid.", code="COHORT_INVITE_INVALID")
    cohort = db.scalar(select(Cohort).where(Cohort.invite_code == invite_code))
    if cohort is None:
        raise NotFoundError(
            "This invite link is invalid or has been turned off.",
            code="COHORT_INVITE_INVALID",
        )
    return join_cohort(db, user=user, cohort=cohort)


def leave_cohort(db: DbSession, *, membership: CohortMembership) -> None:
    """Leave a cohort. The last admin cannot leave without promoting someone."""

    if membership.is_admin and _admin_count(db, membership.cohort_id) <= 1:
        raise ConflictError(
            "You are the only admin. Promote another member before leaving.",
            code="COHORT_LAST_ADMIN",
        )
    audit.record(
        db,
        AuditAction.COHORT_LEFT,
        actor_id=membership.user_id,
        cohort_id=membership.cohort_id,
        entity_type="cohort",
        entity_id=membership.cohort_id,
    )
    db.delete(membership)
    db.flush()


# ---------------------------------------------------------------------------
# Roles
# ---------------------------------------------------------------------------


def _admin_count(db: DbSession, cohort_id: uuid.UUID) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(CohortMembership)
            .where(
                CohortMembership.cohort_id == cohort_id,
                CohortMembership.role == UserRole.ADMIN,
            )
        )
        or 0
    )


def set_member_role(
    db: DbSession,
    *,
    actor: CohortMembership,
    target: CohortMembership,
    role: UserRole,
) -> CohortMembership:
    """Change a member's role within a cohort. Cohort admins only."""

    if not actor.is_admin:
        raise PermissionDeniedError("Only a cohort admin can change roles.")
    if actor.cohort_id != target.cohort_id:
        raise PermissionDeniedError("That member is in a different cohort.")
    if target.role is role:
        return target
    # Do not let the cohort lose its last admin.
    if target.is_admin and role is not UserRole.ADMIN and _admin_count(db, target.cohort_id) <= 1:
        raise ConflictError(
            "A cohort must keep at least one admin.", code="COHORT_LAST_ADMIN"
        )
    previous = target.role
    target.role = role
    audit.record(
        db,
        AuditAction.COHORT_MEMBER_ROLE_CHANGED,
        actor_id=actor.user_id,
        cohort_id=target.cohort_id,
        entity_type="cohort_membership",
        entity_id=target.id,
        context={"user": str(target.user_id), "from": previous.value, "to": role.value},
    )
    db.flush()
    return target


def list_members(
    db: DbSession, *, cohort: Cohort, limit: int = 200, offset: int = 0
) -> tuple[list[CohortMembership], int]:
    stmt = (
        select(CohortMembership)
        .options(selectinload(CohortMembership.skills))
        .join(User, User.id == CohortMembership.user_id)
        .where(CohortMembership.cohort_id == cohort.id, User.is_active.is_(True))
    )
    total = int(db.scalar(select(func.count()).select_from(stmt.subquery())) or 0)
    rows = list(
        db.scalars(stmt.order_by(User.display_name.asc()).limit(limit).offset(offset)).all()
    )
    return rows, total


# ---------------------------------------------------------------------------
# Invite links
# ---------------------------------------------------------------------------


def generate_invite_code(db: DbSession, *, actor: CohortMembership, cohort: Cohort) -> str:
    if not actor.is_admin:
        raise PermissionDeniedError("Only a cohort admin can manage the invite link.")
    cohort.invite_code = generate_token(18)
    audit.record(
        db,
        AuditAction.COHORT_INVITE_GENERATED,
        actor_id=actor.user_id,
        cohort_id=cohort.id,
        entity_type="cohort",
        entity_id=cohort.id,
    )
    db.flush()
    return cohort.invite_code


def revoke_invite_code(db: DbSession, *, actor: CohortMembership, cohort: Cohort) -> None:
    if not actor.is_admin:
        raise PermissionDeniedError("Only a cohort admin can manage the invite link.")
    if cohort.invite_code is None:
        return
    cohort.invite_code = None
    audit.record(
        db,
        AuditAction.COHORT_INVITE_REVOKED,
        actor_id=actor.user_id,
        cohort_id=cohort.id,
        entity_type="cohort",
        entity_id=cohort.id,
    )
    db.flush()
