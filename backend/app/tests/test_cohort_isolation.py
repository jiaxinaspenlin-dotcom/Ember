"""Cross-cohort isolation: the core promise of multi-tenancy.

A member of one cohort must never see, reach, or act on another cohort's
content -- not through a service call, not through the API, not through a
guessed identifier. These tests build two independent cohorts and assert the
wall between them holds.
"""

from __future__ import annotations

import pytest

from app.core.enums import UserRole
from app.core.errors import NotFoundError, PermissionDeniedError
from app.services import (
    announcements,
    channels,
    cohorts,
    decisions,
    direct_messages,
    help_requests,
    messages,
    tasks,
)


@pytest.fixture
def two_cohorts(db, make_cohort, make_user):
    """Two separate cohorts, each with its own admin and channel."""

    cohort_a = make_cohort("Cohort Alpha")
    cohort_b = make_cohort("Cohort Beta")
    alice = make_user("Alice Alpha", admin=True, cohort=cohort_a)
    bob = make_user("Bob Beta", admin=True, cohort=cohort_b)
    channel_a = channels.create_channel(db, actor=alice.membership, name="Alpha Room")
    channel_b = channels.create_channel(db, actor=bob.membership, name="Beta Room")
    db.commit()
    return {
        "a": cohort_a,
        "b": cohort_b,
        "alice": alice,
        "bob": bob,
        "channel_a": channel_a,
        "channel_b": channel_b,
    }


# ---------------------------------------------------------------------------
# Service layer
# ---------------------------------------------------------------------------


def test_a_channel_is_invisible_to_another_cohort(db, two_cohorts):
    # Bob (cohort B) cannot fetch Alice's channel (cohort A) -- it 404s.
    with pytest.raises(NotFoundError) as exc:
        channels.get_channel(db, two_cohorts["b"], two_cohorts["channel_a"].id)
    assert exc.value.code == "CHANNEL_NOT_FOUND"


def test_a_message_is_a_404_across_cohorts(db, two_cohorts):
    alice, bob = two_cohorts["alice"], two_cohorts["bob"]
    message = messages.create_message(
        db, actor=alice.membership, channel=two_cohorts["channel_a"], body="Alpha secret"
    )
    db.commit()

    with pytest.raises(NotFoundError):
        messages.get_visible_message(db, message_id=message.id, actor=bob.membership)


def test_channel_listings_are_scoped_to_the_active_cohort(db, two_cohorts):
    a_items, _ = channels.list_channels(
        db, cohort=two_cohorts["a"], user=two_cohorts["alice"].user
    )
    b_items, _ = channels.list_channels(
        db, cohort=two_cohorts["b"], user=two_cohorts["bob"].user
    )
    a_names = {item.channel.name for item in a_items}
    b_names = {item.channel.name for item in b_items}
    assert "Alpha Room" in a_names and "Beta Room" not in a_names
    assert "Beta Room" in b_names and "Alpha Room" not in b_names


def test_help_decisions_tasks_announcements_never_cross(db, two_cohorts):
    alice, bob = two_cohorts["alice"], two_cohorts["bob"]

    help_a = help_requests.create_help_request(
        db, requester=alice.membership, title="Alpha help", description="x"
    )
    decision_a = decisions.create_decision(
        db, author=alice.membership, title="Alpha decision", decision_text="x"
    )
    task_a = tasks.create_task(db, creator=alice.membership, title="Alpha task")
    ann_a = announcements.create_announcement(
        db, author=alice.membership, title="Alpha announcement", body="x"
    )
    db.commit()

    # Cohort B (Bob) 404s on every one of cohort A's records.
    with pytest.raises(NotFoundError):
        help_requests.get_help_request(db, two_cohorts["b"], help_a.id)
    with pytest.raises(NotFoundError):
        decisions.get_decision(db, two_cohorts["b"], decision_a.id)
    with pytest.raises(NotFoundError):
        tasks.get_task(db, two_cohorts["b"], task_a.id)
    with pytest.raises(NotFoundError):
        announcements.get_announcement(db, two_cohorts["b"], ann_a.id)

    # And B's listings never contain A's rows.
    b_help, _ = help_requests.list_help_requests(
        db,
        cohort=two_cohorts["b"],
        user=bob.user,
        filters=help_requests.HelpFilters(),
    )
    assert help_a.id not in {h.id for h in b_help}
    b_ann, _ = announcements.list_announcements(db, cohort=two_cohorts["b"])
    assert ann_a.id not in {a.id for a in b_ann}


def test_cannot_dm_a_member_of_another_cohort(db, two_cohorts):
    # Alice (A) tries to open a DM with Bob (B): Bob is not in her cohort.
    with pytest.raises((NotFoundError, PermissionDeniedError)):
        direct_messages.get_or_create_conversation(
            db, actor=two_cohorts["alice"].membership, other_user_id=two_cohorts["bob"].user.id
        )


def test_posting_into_another_cohorts_channel_is_refused(db, two_cohorts):
    # Even with a handle on B's channel object, A's actor cannot post to it.
    with pytest.raises((NotFoundError, PermissionDeniedError)):
        messages.create_message(
            db,
            actor=two_cohorts["alice"].membership,
            channel=two_cohorts["channel_b"],
            body="Trespassing",
        )


# ---------------------------------------------------------------------------
# API layer
# ---------------------------------------------------------------------------


def test_api_404s_on_a_cross_cohort_channel_id(client, db, two_cohorts, sign_in):
    # Bob signs in (his single cohort auto-selects) and probes A's channel id.
    sign_in(two_cohorts["bob"])
    response = client.get(f"/api/channels/{two_cohorts['channel_a'].id}")
    assert response.status_code == 404


def test_api_channel_list_only_shows_your_cohort(client, db, two_cohorts, sign_in):
    sign_in(two_cohorts["bob"])
    names = {item["channel"]["name"] for item in client.get("/api/channels").json()["items"]}
    assert "Beta Room" in names
    assert "Alpha Room" not in names


def test_switching_cohorts_changes_what_you_see(client, db, make_cohort, make_user, sign_in):
    # A user who belongs to two cohorts sees each cohort's channels only while active.
    cohort_a = make_cohort("Switch Alpha")
    cohort_b = make_cohort("Switch Beta")
    user = make_user("Poly Member", admin=True, cohort=cohort_a)
    membership_b = cohorts._add_membership(
        db, cohort=cohort_b, user=user.user, role=UserRole.ADMIN
    )
    membership_b.profile_completed = True
    channels.create_channel(db, actor=user.membership, name="Only In Alpha")
    channels.create_channel(db, actor=membership_b, name="Only In Beta")
    db.commit()

    sign_in(user, cohort=cohort_a)
    a_names = {item["channel"]["name"] for item in client.get("/api/channels").json()["items"]}
    assert "Only In Alpha" in a_names and "Only In Beta" not in a_names

    client.post(f"/cohorts/{cohort_b.slug}/switch", follow_redirects=False)
    b_names = {item["channel"]["name"] for item in client.get("/api/channels").json()["items"]}
    assert "Only In Beta" in b_names and "Only In Alpha" not in b_names


def test_no_active_cohort_yields_409(client, db, make_cohort, make_user, sign_in):
    # A user in two cohorts with none chosen must pick one first.
    cohort_a = make_cohort("Ambiguous A")
    cohort_b = make_cohort("Ambiguous B")
    user = make_user("Undecided", cohort=cohort_a)
    cohorts._add_membership(db, cohort=cohort_b, user=user.user, role=user.membership.role)
    db.commit()

    sign_in(user)  # no cohort= -> auto-select fails (two cohorts)
    response = client.get("/api/channels")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "NO_ACTIVE_COHORT"
