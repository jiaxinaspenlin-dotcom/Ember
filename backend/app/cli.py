"""Administrative command line for Ember.

    ember-admin list-users
    ember-admin grant-admin --email someone@example.com --cohort summer-2026
    ember-admin revoke-admin --github someuser --cohort summer-2026
    ember-admin stats
    ember-admin purge-sessions

Roles are per-cohort. Role changes happen *only* here or through the
authenticated cohort-admin API -- never from a request payload, URL parameter
or browser state.
"""

from __future__ import annotations

import argparse
import sys

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.enums import UserRole
from app.core.security import normalize_email
from app.db.session import session_scope
from app.models.action import Decision, HelpRequest, Task
from app.models.channel import Channel
from app.models.cohort import Cohort, CohortMembership
from app.models.message import Message
from app.models.user import OAuthAccount, User


def _find_user(db: Session, *, email: str | None, github: str | None) -> User | None:
    if email:
        return db.scalar(select(User).where(User.email == normalize_email(email)))
    if github:
        account = db.scalar(
            select(OAuthAccount).where(
                OAuthAccount.provider == "github",
                func.lower(OAuthAccount.provider_username) == github.lower(),
            )
        )
        return db.get(User, account.user_id) if account else None
    return None


def _set_role(args: argparse.Namespace, role: UserRole) -> int:
    """Set a user's role *within one cohort*. Roles are per-cohort now."""

    with session_scope() as db:
        user = _find_user(db, email=args.email, github=args.github)
        if user is None:
            print("No matching user found.", file=sys.stderr)
            return 1
        cohort = db.scalar(select(Cohort).where(Cohort.slug == args.cohort))
        if cohort is None:
            print(f"No cohort with slug {args.cohort!r}.", file=sys.stderr)
            return 1
        membership = db.scalar(
            select(CohortMembership).where(
                CohortMembership.cohort_id == cohort.id,
                CohortMembership.user_id == user.id,
            )
        )
        if membership is None:
            print(
                f"{user.display_name} is not a member of {cohort.name}.", file=sys.stderr
            )
            return 1
        if membership.role is role:
            print(f"{user.display_name} is already {role.value} in {cohort.name}.")
            return 0
        # Never strand a cohort without an admin.
        if role is not UserRole.ADMIN and membership.is_admin:
            admins = int(
                db.scalar(
                    select(func.count())
                    .select_from(CohortMembership)
                    .where(
                        CohortMembership.cohort_id == cohort.id,
                        CohortMembership.role == UserRole.ADMIN,
                    )
                )
                or 0
            )
            if admins <= 1:
                print(
                    f"{cohort.name} must keep at least one admin.", file=sys.stderr
                )
                return 1
        membership.role = role
        print(f"{user.display_name} is now {role.value} in {cohort.name}.")
    return 0


def _list_users(_: argparse.Namespace) -> int:
    with session_scope() as db:
        users = db.scalars(select(User).order_by(User.created_at.asc())).all()
        if not users:
            print("No users yet. The database is empty.")
            return 0
        for user in users:
            memberships = db.scalars(
                select(CohortMembership).where(CohortMembership.user_id == user.id)
            ).all()
            cohort_count = len(memberships)
            admin_of = sum(1 for m in memberships if m.role is UserRole.ADMIN)
            summary = f"{cohort_count} cohort(s), admin of {admin_of}"
            print(
                f"{user.display_name:<28}  {user.email or '(no email)':<32}  {summary}"
            )
    return 0


def _stats(_: argparse.Namespace) -> int:
    with session_scope() as db:

        def count(model: type) -> int:
            return int(db.scalar(select(func.count()).select_from(model)) or 0)

        print(f"users:          {count(User)}")
        print(f"cohorts:        {count(Cohort)}")
        print(f"memberships:    {count(CohortMembership)}")
        print(f"channels:       {count(Channel)}")
        print(f"messages:       {count(Message)}")
        print(f"help requests:  {count(HelpRequest)}")
        print(f"decisions:      {count(Decision)}")
        print(f"tasks:          {count(Task)}")
    return 0


def _purge_sessions(_: argparse.Namespace) -> int:
    import datetime as dt

    from sqlalchemy import delete

    from app.auth import sessions as session_service
    from app.auth.github import purge_expired_states
    from app.db.base import rows_affected, utcnow
    from app.models.user import LoginAttempt
    from app.services.credentials import purge_expired_tokens

    with session_scope() as db:
        removed = session_service.purge_expired_sessions(db)
        states = purge_expired_states(db)
        tokens = purge_expired_tokens(db)
        # Rate-limit rows are only meaningful inside the attempt window.
        attempts = rows_affected(
            db.execute(
                delete(LoginAttempt).where(
                    LoginAttempt.created_at < utcnow() - dt.timedelta(days=1)
                )
            )
        )
    print(
        f"Removed {removed} expired sessions, {states} stale OAuth states, "
        f"{tokens} expired email tokens and {attempts} old rate-limit records."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ember-admin", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, help_text in (
        ("grant-admin", "Grant the administrator role within a cohort"),
        ("revoke-admin", "Return a user to the member role within a cohort"),
    ):
        cmd = sub.add_parser(name, help=help_text)
        cmd.add_argument("--email", help="Account email address")
        cmd.add_argument("--github", help="GitHub username")
        cmd.add_argument(
            "--cohort", required=True, help="Cohort slug the role applies to"
        )

    sub.add_parser("list-users", help="List all accounts and roles")
    sub.add_parser("stats", help="Show record counts")
    sub.add_parser("purge-sessions", help="Delete long-expired sessions and OAuth states")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "grant-admin":
        return _set_role(args, UserRole.ADMIN)
    if args.command == "revoke-admin":
        return _set_role(args, UserRole.MEMBER)
    if args.command == "list-users":
        return _list_users(args)
    if args.command == "stats":
        return _stats(args)
    if args.command == "purge-sessions":
        return _purge_sessions(args)
    parser.error(f"Unknown command {args.command}")
    return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
