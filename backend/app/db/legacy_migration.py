"""One-time bridge: upgrade a pre-tenancy Ember database to multi-tenancy.

The multi-tenant schema shipped as a fresh Alembic baseline, so a database from
an earlier single-tenant build cannot ``alembic upgrade`` onto it. This module
transforms such a database **in place, preserving its data**:

1. Move every existing table (and the ``message_seq`` sequence) into a `legacy`
   Postgres schema.
2. Build the canonical multi-tenant schema in `public` from the ORM metadata.
3. Copy data across. A single **default cohort** is created; every user's global
   ``role`` + ``profiles`` row becomes their :class:`CohortMembership` in it, and
   every tenant-owned row is stamped with the default ``cohort_id``. Columns that
   no longer exist (``users.role``, ``users.profile_completed``) simply fall away;
   the new ``cohort_id`` column is injected.
4. Stamp Alembic at the current head and drop the `legacy` schema.

Run it once, against a backup, via ``python -m app.db.legacy_migration``. It is
idempotent-guarded: it refuses to run unless the database is on the old schema
(has ``profiles``, lacks ``cohorts``).
"""

from __future__ import annotations

import uuid

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app.core.enums import UserRole, WorkingStatus
from app.core.logging import get_logger
from app.db.session import engine as default_engine
from app.models import Base
from app.models.message import Message

logger = get_logger("ember.legacy-migration")

# Tables that have no legacy source -- they are synthesised from legacy data.
_SYNTHESISED = {"cohorts", "cohort_memberships", "membership_skills"}


class NotALegacyDatabase(RuntimeError):
    """The database is not a pre-tenancy single-tenant Ember database."""


def _quote_cols(names: list[str]) -> str:
    return ", ".join(f'"{n}"' for n in names)


def upgrade_legacy_to_multitenant(
    engine: Engine | None = None,
    *,
    cohort_name: str = "Cohort",
    cohort_slug: str = "cohort",
) -> uuid.UUID:
    """Transform a single-tenant database into the multi-tenant schema.

    Returns the id of the default cohort every existing row is moved into.
    """

    engine = engine or default_engine
    insp = inspect(engine)
    existing = set(insp.get_table_names())
    if "cohorts" in existing:
        raise NotALegacyDatabase("Database already has a `cohorts` table.")
    if "profiles" not in existing:
        raise NotALegacyDatabase("Database has no `profiles` table to migrate.")

    legacy_tables = sorted(existing)
    legacy_sequences = _sequence_names(engine)

    # 1. Park the old schema out of the way.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS legacy CASCADE"))
        conn.execute(text("CREATE SCHEMA legacy"))
        for legacy_table in legacy_tables:
            conn.execute(text(f'ALTER TABLE public."{legacy_table}" SET SCHEMA legacy'))
        for seq in legacy_sequences:
            conn.execute(text(f'ALTER SEQUENCE public."{seq}" SET SCHEMA legacy'))

    # 2. Build the canonical multi-tenant schema.
    Base.metadata.create_all(bind=engine)

    legacy_insp = inspect(engine)
    default_cohort_id = uuid.uuid4()

    with engine.begin() as conn:
        # The one cohort every existing row belongs to. No creator (system-made).
        conn.execute(
            text(
                "INSERT INTO public.cohorts (id, slug, name, created_by_id) "
                "VALUES (:id, :slug, :name, NULL)"
            ),
            {"id": default_cohort_id, "slug": cohort_slug, "name": cohort_name},
        )

        # Copy every table in dependency order. Synthesised tables are handled
        # bespoke; everything else is a column-intersection copy.
        for table in Base.metadata.sorted_tables:
            name = table.name
            if name in _SYNTHESISED:
                continue
            if name not in _legacy_names(legacy_insp):
                continue  # a brand-new table with no legacy source
            _copy_table(conn, legacy_insp, table, cohort_id=default_cohort_id)

        membership_by_user = _build_memberships(conn, default_cohort_id)
        _build_membership_skills(conn, legacy_insp, membership_by_user)
        _reset_message_seq(conn)
        _stamp_alembic_head(conn)

    # 3. Drop the parked schema.
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA legacy CASCADE"))

    logger.info(
        "Legacy upgrade complete: %d users moved into cohort %s",
        len(membership_by_user),
        default_cohort_id,
    )
    return default_cohort_id


def _sequence_names(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'"
            )
        ).all()
    return [r[0] for r in rows]


def _legacy_names(legacy_insp) -> set[str]:  # type: ignore[no-untyped-def]
    return set(legacy_insp.get_table_names(schema="legacy"))


def _copy_table(conn, legacy_insp, table, *, cohort_id: uuid.UUID) -> None:  # type: ignore[no-untyped-def]
    """Copy the columns a legacy and current table share; inject ``cohort_id``."""

    legacy_cols = {c["name"] for c in legacy_insp.get_columns(table.name, schema="legacy")}
    new_cols = [c.name for c in table.columns]
    shared = [c for c in new_cols if c in legacy_cols]
    if not shared:
        return

    select_cols = list(shared)
    insert_cols = list(shared)
    params: dict[str, object] = {}
    # Tenant tables gain a non-null cohort_id that legacy rows never had.
    if "cohort_id" in new_cols and "cohort_id" not in legacy_cols:
        insert_cols.append("cohort_id")
        select_cols.append(":cohort_id")
        params["cohort_id"] = cohort_id

    select_sql = ", ".join(
        c if c.startswith(":") else _quote_cols([c]) for c in select_cols
    )
    stmt = (
        f'INSERT INTO public."{table.name}" ({_quote_cols(insert_cols)}) '
        f'SELECT {select_sql} FROM legacy."{table.name}"'
    )
    conn.execute(text(stmt), params)


def _build_memberships(conn, cohort_id: uuid.UUID) -> dict[uuid.UUID, uuid.UUID]:  # type: ignore[no-untyped-def]
    """One membership per legacy user, carrying their role + profile."""

    rows = conn.execute(
        text(
            """
            SELECT u.id            AS user_id,
                   u.role          AS role,
                   u.profile_completed AS profile_completed,
                   u.created_at    AS created_at,
                   p.bio           AS bio,
                   p.current_project AS current_project,
                   p.project_area  AS project_area,
                   p.working_status AS working_status,
                   p.available_to_help AS available_to_help
            FROM legacy.users u
            LEFT JOIN legacy.profiles p ON p.user_id = u.id
            """
        )
    ).mappings().all()

    mapping: dict[uuid.UUID, uuid.UUID] = {}
    for row in rows:
        membership_id = uuid.uuid4()
        mapping[row["user_id"]] = membership_id
        conn.execute(
            text(
                """
                INSERT INTO public.cohort_memberships
                    (id, cohort_id, user_id, role, joined_at,
                     bio, current_project, project_area, working_status,
                     available_to_help, profile_completed)
                VALUES
                    (:id, :cohort_id, :user_id, :role, :joined_at,
                     :bio, :current_project, :project_area, :working_status,
                     :available_to_help, :profile_completed)
                """
            ),
            {
                "id": membership_id,
                "cohort_id": cohort_id,
                "user_id": row["user_id"],
                "role": (row["role"] or UserRole.MEMBER.value),
                "joined_at": row["created_at"],
                "bio": row["bio"],
                "current_project": row["current_project"],
                "project_area": row["project_area"],
                "working_status": row["working_status"] or WorkingStatus.BUILDING.value,
                "available_to_help": bool(row["available_to_help"]),
                "profile_completed": bool(row["profile_completed"]),
            },
        )
    return mapping


def _build_membership_skills(conn, legacy_insp, membership_by_user) -> None:  # type: ignore[no-untyped-def]
    """Re-home legacy `profile_skills` onto the new memberships."""

    if "profile_skills" not in _legacy_names(legacy_insp):
        return
    rows = conn.execute(
        text(
            "SELECT profile_user_id, skill_id, position FROM legacy.profile_skills"
        )
    ).mappings().all()
    for row in rows:
        membership_id = membership_by_user.get(row["profile_user_id"])
        if membership_id is None:
            continue
        conn.execute(
            text(
                "INSERT INTO public.membership_skills (membership_id, skill_id, position) "
                "VALUES (:membership_id, :skill_id, :position)"
            ),
            {
                "membership_id": membership_id,
                "skill_id": row["skill_id"],
                "position": row["position"],
            },
        )


def _reset_message_seq(conn) -> None:  # type: ignore[no-untyped-def]
    """Point the fresh sequence past the highest imported message seq."""

    max_seq = conn.execute(text("SELECT COALESCE(MAX(seq), 0) FROM public.messages")).scalar()
    if max_seq:
        conn.execute(text("SELECT setval('message_seq', :v)"), {"v": int(max_seq)})
    _ = Message  # imported so the sequence is registered on the metadata above


def _stamp_alembic_head(conn) -> None:  # type: ignore[no-untyped-def]
    """Mark the database as being at the current migration head."""

    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config("alembic.ini")
    head = ScriptDirectory.from_config(cfg).get_current_head()
    conn.execute(
        text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)")
    )
    conn.execute(text("DELETE FROM alembic_version"))
    conn.execute(
        text("INSERT INTO alembic_version (version_num) VALUES (:v)"), {"v": head}
    )


def _main() -> int:
    logger.info("Starting legacy -> multi-tenant upgrade (back up first!)")
    cohort_id = upgrade_legacy_to_multitenant()
    logger.info("Done. Default cohort: %s", cohort_id)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
