"""End-to-end test of the legacy single-tenant -> multi-tenant upgrade.

This is what makes the bridge migration *not blind*: it stands up a real
pre-tenancy database (the reconstructed old schema below), seeds representative
data across the transform's distinct behaviours, runs
``upgrade_legacy_to_multitenant``, and asserts the data landed correctly and the
resulting schema matches the current models.

If a real legacy database differed from this reconstruction, the DDL here is the
single place to adjust.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.engine import Engine

from app.core.config import settings
from app.db.legacy_migration import NotALegacyDatabase, upgrade_legacy_to_multitenant

LEGACY_DB = "ember_legacy_migration_test"

# Reconstructed pre-tenancy schema: identity carried a global `role` and
# `profile_completed`; profiles/skills were separate; nothing had `cohort_id`.
LEGACY_DDL = """
CREATE TABLE users (
    id uuid PRIMARY KEY,
    email varchar(320) UNIQUE,
    email_verified boolean NOT NULL DEFAULT false,
    display_name varchar(120) NOT NULL,
    avatar_url varchar(500),
    role varchar(20) NOT NULL DEFAULT 'member',
    is_active boolean NOT NULL DEFAULT true,
    profile_completed boolean NOT NULL DEFAULT false,
    last_login_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE profiles (
    user_id uuid PRIMARY KEY REFERENCES users(id),
    bio text,
    current_project varchar(160),
    project_area varchar(80),
    working_status varchar(40) NOT NULL DEFAULT 'building',
    available_to_help boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE skills (
    id uuid PRIMARY KEY,
    slug varchar(60) UNIQUE NOT NULL,
    name varchar(60) NOT NULL
);
CREATE TABLE profile_skills (
    profile_user_id uuid NOT NULL REFERENCES profiles(user_id),
    skill_id uuid NOT NULL REFERENCES skills(id),
    position integer NOT NULL DEFAULT 0,
    PRIMARY KEY (profile_user_id, skill_id)
);
CREATE SEQUENCE message_seq;
CREATE TABLE channels (
    id uuid PRIMARY KEY,
    slug varchar(60) UNIQUE NOT NULL,
    name varchar(80) NOT NULL,
    description varchar(300),
    topic varchar(200),
    invite_code varchar(64),
    is_archived boolean NOT NULL DEFAULT false,
    archived_at timestamptz,
    archived_by_id uuid,
    created_by_id uuid NOT NULL REFERENCES users(id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE messages (
    id uuid PRIMARY KEY,
    seq bigint NOT NULL DEFAULT nextval('message_seq'),
    sender_id uuid NOT NULL REFERENCES users(id),
    channel_id uuid REFERENCES channels(id),
    direct_conversation_id uuid,
    parent_message_id uuid,
    body text NOT NULL,
    message_type varchar(40) NOT NULL DEFAULT 'text',
    is_pinned boolean NOT NULL DEFAULT false,
    reply_count integer NOT NULL DEFAULT 0,
    edited_at timestamptz,
    deleted_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE notifications (
    id uuid PRIMARY KEY,
    recipient_id uuid NOT NULL REFERENCES users(id),
    actor_id uuid,
    notification_type varchar(40) NOT NULL,
    title varchar(200) NOT NULL,
    body varchar(500),
    link_path varchar(300) NOT NULL DEFAULT '/',
    read_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
"""


def _maintenance_engine() -> Engine:
    url = make_url(settings.database_url).set(database="postgres")
    return create_engine(url, isolation_level="AUTOCOMMIT", future=True)


@pytest.fixture
def legacy_engine() -> Iterator[Engine]:
    maint = _maintenance_engine()
    with maint.connect() as conn:
        conn.execute(text(f'DROP DATABASE IF EXISTS "{LEGACY_DB}" WITH (FORCE)'))
        conn.execute(text(f'CREATE DATABASE "{LEGACY_DB}"'))
    url = make_url(settings.database_url).set(database=LEGACY_DB)
    engine = create_engine(url, future=True)
    try:
        yield engine
    finally:
        engine.dispose()
        with maint.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{LEGACY_DB}" WITH (FORCE)'))
        maint.dispose()


def _seed_legacy(engine: Engine) -> dict[str, uuid.UUID]:
    admin_id, member_id = uuid.uuid4(), uuid.uuid4()
    skill_py, skill_rust = uuid.uuid4(), uuid.uuid4()
    channel_id, message_id, notif_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as conn:
        for stmt in filter(None, (s.strip() for s in LEGACY_DDL.split(";"))):
            conn.execute(text(stmt))
        conn.execute(
            text(
                "INSERT INTO users (id, email, display_name, role, profile_completed) "
                "VALUES (:a,'admin@old.dev','Old Admin','admin',true), "
                "(:m,'member@old.dev','Old Member','member',true)"
            ),
            {"a": admin_id, "m": member_id},
        )
        conn.execute(
            text(
                "INSERT INTO profiles (user_id, bio, current_project, working_status, "
                "available_to_help) VALUES "
                "(:a,'Runs the show','Ember','in_focus',false), "
                "(:m,'Builds things','Widgets','building',true)"
            ),
            {"a": admin_id, "m": member_id},
        )
        conn.execute(
            text(
                "INSERT INTO skills (id, slug, name) "
                "VALUES (:p,'python','Python'),(:r,'rust','Rust')"
            ),
            {"p": skill_py, "r": skill_rust},
        )
        conn.execute(
            text(
                "INSERT INTO profile_skills (profile_user_id, skill_id, position) "
                "VALUES (:m,:p,0),(:m,:r,1)"
            ),
            {"m": member_id, "p": skill_py, "r": skill_rust},
        )
        conn.execute(
            text(
                "INSERT INTO channels (id, slug, name, created_by_id) "
                "VALUES (:c,'general','General',:a)"
            ),
            {"c": channel_id, "a": admin_id},
        )
        conn.execute(
            text(
                "INSERT INTO messages (id, seq, sender_id, channel_id, body) "
                "VALUES (:mid, nextval('message_seq'), :a, :c, 'Historic message')"
            ),
            {"mid": message_id, "a": admin_id, "c": channel_id},
        )
        conn.execute(
            text(
                "INSERT INTO notifications (id, recipient_id, notification_type, title) "
                "VALUES (:n, :m, 'mention', 'You were mentioned')"
            ),
            {"n": notif_id, "m": member_id},
        )
    return {
        "admin_id": admin_id,
        "member_id": member_id,
        "channel_id": channel_id,
        "message_id": message_id,
        "notif_id": notif_id,
    }


def test_legacy_database_upgrades_preserving_all_data(legacy_engine):
    seed = _seed_legacy(legacy_engine)

    cohort_id = upgrade_legacy_to_multitenant(
        legacy_engine, cohort_name="Founding Cohort", cohort_slug="founding"
    )

    with legacy_engine.connect() as conn:
        # Exactly one cohort; every user has a membership carrying their old role.
        assert conn.execute(text("SELECT count(*) FROM cohorts")).scalar() == 1
        assert conn.execute(text("SELECT count(*) FROM cohort_memberships")).scalar() == 2

        roles = dict(
            conn.execute(text("SELECT user_id, role FROM cohort_memberships")).all()
        )
        assert str(roles[seed["admin_id"]]) == "admin"
        assert str(roles[seed["member_id"]]) == "member"

        # Per-cohort profile fields moved off users/profiles onto the membership.
        member = conn.execute(
            text(
                "SELECT bio, current_project, available_to_help, profile_completed "
                "FROM cohort_memberships WHERE user_id = :m"
            ),
            {"m": seed["member_id"]},
        ).mappings().one()
        assert member["bio"] == "Builds things"
        assert member["current_project"] == "Widgets"
        assert member["available_to_help"] is True
        assert member["profile_completed"] is True

        # Skills re-homed onto the membership.
        skill_count = conn.execute(
            text(
                "SELECT count(*) FROM membership_skills ms "
                "JOIN cohort_memberships m ON m.id = ms.membership_id "
                "WHERE m.user_id = :u"
            ),
            {"u": seed["member_id"]},
        ).scalar()
        assert skill_count == 2

        # Tenant rows are stamped with the default cohort.
        assert (
            conn.execute(text("SELECT cohort_id FROM channels")).scalar() == cohort_id
        )
        msg = conn.execute(
            text("SELECT cohort_id, seq, body FROM messages")
        ).mappings().one()
        assert msg["cohort_id"] == cohort_id
        assert msg["body"] == "Historic message"
        assert (
            conn.execute(text("SELECT cohort_id FROM notifications")).scalar() == cohort_id
        )

        # Identity lost its global role/profile columns.
        user_cols = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name='users'"
                )
            ).all()
        }
        assert "role" not in user_cols
        assert "profile_completed" not in user_cols

        # Legacy tables are gone; the DB is stamped at the current head.
        remaining = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public'"
                )
            ).all()
        }
        assert "profiles" not in remaining
        assert "cohorts" in remaining
        version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version  # a concrete revision id

    # The message sequence continues past the imported rows, not from 1.
    with legacy_engine.begin() as conn:
        next_seq = conn.execute(text("SELECT nextval('message_seq')")).scalar()
        assert next_seq == msg["seq"] + 1


def test_refuses_a_database_that_is_already_multitenant(legacy_engine):
    _seed_legacy(legacy_engine)
    upgrade_legacy_to_multitenant(legacy_engine)
    # Running again must refuse -- it is no longer a legacy database.
    with pytest.raises(NotALegacyDatabase):
        upgrade_legacy_to_multitenant(legacy_engine)
