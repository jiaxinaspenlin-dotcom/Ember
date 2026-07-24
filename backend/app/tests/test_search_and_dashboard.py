"""Search behaviour, result limits, and the lightweight dashboard summary."""

from __future__ import annotations

import datetime as dt

import pytest

from app.db.base import utcnow
from app.search.queries import SearchFilters, SearchScope, build_excerpt, normalize_query, search
from app.services import (
    announcements,
    channels,
    dashboard,
    decisions,
    direct_messages,
    help_requests,
    messages,
    profiles,
    tasks,
)


@pytest.fixture
def cohort(db, make_user, make_channel):
    admin = make_user("Ada Admin", admin=True)
    maya = make_user("Maya Chen")
    sam = make_user("Sam Okoro")
    channel = make_channel(admin, "Build Log")
    for member in (maya, sam):
        channels.join_channel(db, actor=member, channel=channel)
    db.commit()
    return {"admin": admin, "maya": maya, "sam": sam, "channel": channel}


# ---------------------------------------------------------------------------
# Query construction helpers (pure Python)
# ---------------------------------------------------------------------------


def test_normalize_query_trims_and_collapses():
    assert normalize_query("  hello    world  ") == "hello world"
    assert normalize_query("") == ""
    assert len(normalize_query("x" * 500)) == 200


def test_build_excerpt_centres_on_the_match():
    body = "alpha " * 40 + "needle " + "omega " * 40
    excerpt = build_excerpt(body, "needle")
    assert "needle" in excerpt
    assert excerpt.startswith("…")
    assert len(excerpt) < len(body)


def test_build_excerpt_without_a_match_still_returns_text():
    assert build_excerpt("some short body", "absent") == "some short body"


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------


def test_keyword_search_finds_channel_messages(db, cohort):
    messages.create_message(
        db,
        actor=cohort["maya"],
        channel=cohort["channel"],
        body="We should benchmark the ingestion pipeline",
    )
    messages.create_message(
        db, actor=cohort["sam"], channel=cohort["channel"], body="Unrelated chatter"
    )
    db.commit()

    response = search(
        db,
        cohort_id=cohort["sam"].cohort_id,
        user=cohort["sam"],
        filters=SearchFilters(query="benchmark ingestion"),
    )
    assert response.total == 1
    assert response.results[0].kind == "message"
    assert "benchmark" in response.results[0].excerpt


def test_empty_query_returns_an_empty_response(db, cohort):
    response = search(
        db,
        cohort_id=cohort["maya"].cohort_id,
        user=cohort["maya"],
        filters=SearchFilters(query="   "),
    )
    assert response.results == []
    assert response.total == 0


def test_search_excludes_deleted_messages(db, cohort):
    message = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="ephemeral kumquat"
    )
    db.commit()
    assert (
        search(
            db,
            cohort_id=cohort["sam"].cohort_id,
            user=cohort["sam"],
            filters=SearchFilters(query="kumquat"),
        ).total
        == 1
    )

    messages.soft_delete_message(db, actor=cohort["maya"], message=message)
    db.commit()
    assert (
        search(
            db,
            cohort_id=cohort["sam"].cohort_id,
            user=cohort["sam"],
            filters=SearchFilters(query="kumquat"),
        ).total
        == 0
    )


def test_search_filters_by_channel_sender_and_date(db, cohort, make_user, make_channel):
    other_channel = make_channel(cohort["admin"], "Second Channel")
    channels.join_channel(db, actor=cohort["maya"], channel=other_channel)
    db.commit()

    messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="widget in build log"
    )
    messages.create_message(
        db, actor=cohort["maya"], channel=other_channel, body="widget in second channel"
    )
    db.commit()

    both = search(
        db,
        cohort_id=cohort["maya"].cohort_id,
        user=cohort["maya"],
        filters=SearchFilters(query="widget"),
    )
    assert both.total == 2

    scoped = search(
        db,
        cohort_id=cohort["maya"].cohort_id,
        user=cohort["maya"],
        filters=SearchFilters(query="widget", channel_id=other_channel.id),
    )
    assert scoped.total == 1
    assert "second channel" in scoped.results[0].excerpt

    by_sender = search(
        db,
        cohort_id=cohort["maya"].cohort_id,
        user=cohort["maya"],
        filters=SearchFilters(query="widget", sender_id=cohort["sam"].id),
    )
    assert by_sender.total == 0

    future = search(
        db,
        cohort_id=cohort["maya"].cohort_id,
        user=cohort["maya"],
        filters=SearchFilters(query="widget", date_from=utcnow() + dt.timedelta(days=1)),
    )
    assert future.total == 0


def test_search_covers_help_requests_decisions_and_announcements(db, cohort):
    help_requests.create_help_request(
        db,
        requester=cohort["maya"],
        title="Zeppelin deployment question",
        description="How do we roll back?",
    )
    decisions.create_decision(
        db,
        author=cohort["maya"],
        title="Zeppelin stays on version two",
        decision_text="Upgrading is not worth it yet.",
    )
    announcements.create_announcement(
        db, author=cohort["admin"], title="Zeppelin maintenance", body="Downtime on Friday."
    )
    db.commit()

    everything = search(
        db,
        cohort_id=cohort["sam"].cohort_id,
        user=cohort["sam"],
        filters=SearchFilters(query="zeppelin"),
    )
    kinds = {result.kind for result in everything.results}
    assert kinds == {"help_request", "decision", "announcement"}

    only_decisions = search(
        db,
        cohort_id=cohort["sam"].cohort_id,
        user=cohort["sam"],
        filters=SearchFilters(query="zeppelin", scope=SearchScope.DECISIONS),
    )
    assert {r.kind for r in only_decisions.results} == {"decision"}


def test_search_results_are_limited_and_paginated(db, cohort):
    for index in range(30):
        messages.create_message(
            db,
            actor=cohort["maya"],
            channel=cohort["channel"],
            body=f"repeated keyword marker number {index}",
        )
    db.commit()

    first_page = search(
        db,
        cohort_id=cohort["sam"].cohort_id,
        user=cohort["sam"],
        filters=SearchFilters(query="marker", scope=SearchScope.MESSAGES),
        limit=10,
    )
    assert len(first_page.results) == 10
    assert first_page.total == 30
    assert first_page.has_more is True

    second_page = search(
        db,
        cohort_id=cohort["sam"].cohort_id,
        user=cohort["sam"],
        filters=SearchFilters(query="marker", scope=SearchScope.MESSAGES),
        limit=10,
        offset=10,
    )
    assert len(second_page.results) == 10
    assert {r.id for r in first_page.results} & {r.id for r in second_page.results} == set()


def test_search_limit_is_capped(db, cohort):
    response = search(
        db,
        cohort_id=cohort["maya"].cohort_id,
        user=cohort["maya"],
        filters=SearchFilters(query="anything"),
        limit=10_000,
    )
    assert response.limit <= 100


def test_search_includes_own_direct_messages_but_can_be_narrowed(db, cohort):
    conversation = direct_messages.get_or_create_conversation(
        db, actor=cohort["maya"], other_user_id=cohort["sam"].id
    )
    messages.create_message(
        db, actor=cohort["maya"], conversation=conversation, body="secret artichoke plan"
    )
    db.commit()

    with_dms = search(
        db,
        cohort_id=cohort["sam"].cohort_id,
        user=cohort["sam"],
        filters=SearchFilters(query="artichoke"),
    )
    assert with_dms.total == 1

    without_dms = search(
        db,
        cohort_id=cohort["sam"].cohort_id,
        user=cohort["sam"],
        filters=SearchFilters(query="artichoke", include_direct_messages=False),
    )
    assert without_dms.total == 0


def test_search_api_requires_a_query(client, cohort, sign_in):
    sign_in(cohort["maya"])
    assert client.get("/api/search").status_code == 422


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_on_an_empty_workspace(db, make_user):
    user = make_user("Lonely First User")
    summary = dashboard.build_summary(db, ctx=user)

    assert summary.unread_messages == 0
    assert summary.open_help_requests == 0
    assert summary.my_open_tasks == 0
    assert summary.active_channels == 0
    assert summary.has_any_channel is False
    assert summary.recent_announcements == []
    assert summary.open_help_queue == []
    assert summary.recent_decisions == []
    assert summary.my_tasks == []
    assert summary.available_helpers == []
    assert summary.recent_mentions == []
    assert summary.member_count == 1


def test_dashboard_reflects_real_records(db, cohort):
    messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Hello @Sam-Okoro"
    )
    help_requests.create_help_request(
        db, requester=cohort["maya"], title="Need a hand here", description="Details."
    )
    decisions.create_decision(
        db, author=cohort["maya"], title="A recorded decision", decision_text="Text."
    )
    tasks.create_task(
        db, creator=cohort["maya"], title="Do the thing", assignee_id=cohort["sam"].id
    )
    announcements.create_announcement(
        db, author=cohort["admin"], title="An announcement here", body="Body."
    )
    profiles.update_profile(db, membership=cohort["maya"].membership, available_to_help=True)
    db.commit()

    summary = dashboard.build_summary(db, ctx=cohort["sam"])
    assert summary.unread_messages == 1
    assert summary.open_help_requests == 1
    assert summary.my_open_tasks == 1
    assert summary.active_channels == 1
    assert len(summary.recent_announcements) == 1
    assert len(summary.recent_decisions) == 1
    assert len(summary.my_tasks) == 1
    assert [m.user_id for m in summary.available_helpers] == [cohort["maya"].id]
    assert len(summary.recent_mentions) == 1
    assert summary.unread_notifications >= 1


def test_dashboard_endpoint_excludes_other_peoples_private_data(client, db, cohort, sign_in):
    outsider_a = cohort["maya"]
    outsider_b = cohort["admin"]
    conversation = direct_messages.get_or_create_conversation(
        db, actor=outsider_a, other_user_id=outsider_b.id
    )
    messages.create_message(
        db, actor=outsider_a, conversation=conversation, body="confidential rhubarb"
    )
    db.commit()

    sign_in(cohort["sam"])
    payload = client.get("/api/dashboard").text
    assert "rhubarb" not in payload


def test_member_directory_filters(db, cohort):
    profiles.update_profile(
        db,
        membership=cohort["maya"].membership,
        skills=["Python", "Deployment"],
        current_project="Ingestion service",
        project_area="Data",
        available_to_help=True,
    )
    profiles.update_profile(
        db, membership=cohort["sam"].membership, skills=["Design"], project_area="Product"
    )
    db.commit()

    by_skill, _ = profiles.list_directory(
        db, cohort=cohort["admin"].cohort, filters=profiles.DirectoryFilters(skill="python")
    )
    assert [m.user_id for m in by_skill] == [cohort["maya"].id]

    available, _ = profiles.list_directory(
        db, cohort=cohort["admin"].cohort, filters=profiles.DirectoryFilters(available_only=True)
    )
    assert [m.user_id for m in available] == [cohort["maya"].id]

    by_area, _ = profiles.list_directory(
        db, cohort=cohort["admin"].cohort, filters=profiles.DirectoryFilters(project_area="product")
    )
    assert [m.user_id for m in by_area] == [cohort["sam"].id]

    searched, _ = profiles.list_directory(
        db, cohort=cohort["admin"].cohort, filters=profiles.DirectoryFilters(query="ingestion")
    )
    assert [m.user_id for m in searched] == [cohort["maya"].id]


def test_member_directory_never_exposes_email_addresses(client, cohort, sign_in):
    sign_in(cohort["sam"])
    payload = client.get("/api/members").text
    assert cohort["maya"].email not in payload
    assert "@" not in payload.replace("embercohort.dev", "")
