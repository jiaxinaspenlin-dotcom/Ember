"""Presence, kudos, daily check-ins, and the cohort pulse (campfire)."""

from __future__ import annotations

import datetime as dt

import pytest

from app.core.enums import NotificationType, TaskStatus
from app.core.errors import NotFoundError, ValidationError
from app.db.base import utcnow
from app.services import community, messages, notifications, tasks

# ---------------------------------------------------------------------------
# Presence
# ---------------------------------------------------------------------------


def test_presence_buckets_by_recency():
    now = utcnow()
    assert community.presence_for(now, now=now) == "online"
    assert community.presence_for(now - dt.timedelta(minutes=3), now=now) == "online"
    assert community.presence_for(now - dt.timedelta(minutes=15), now=now) == "away"
    assert community.presence_for(now - dt.timedelta(hours=2), now=now) == "offline"
    assert community.presence_for(None, now=now) == "offline"


def test_signing_in_marks_you_online_immediately(db, make_user):
    # Presence must not wait for the 5-minute refresh -- login lights it up.
    from app.auth import sessions

    person = make_user("Fresh Login")
    assert person.user.last_active_at is None
    sessions.create_session(db, person.user)
    db.commit()
    assert person.user.last_active_at is not None
    assert community.count_online(db, cohort=person.cohort) == 1


def test_count_online_only_counts_active_members(db, make_user, make_cohort):
    now = utcnow()
    here = make_user("Present Person")
    here.user.last_active_at = now
    away = make_user("Away Person")
    away.user.last_active_at = now - dt.timedelta(hours=1)
    # A member of a *different* cohort who is online must not be counted.
    other_cohort = make_cohort("Elsewhere")
    make_user("Outsider Online", cohort=other_cohort).user.last_active_at = now
    db.commit()

    assert community.count_online(db, cohort=here.cohort, now=now) == 1


# ---------------------------------------------------------------------------
# Kudos
# ---------------------------------------------------------------------------


def test_give_kudos_records_and_notifies(db, make_user):
    giver = make_user("Grateful Giver")
    receiver = make_user("Helpful Helper")

    community.give_kudos(
        db, actor=giver.membership, to_user_id=receiver.id, message="Saved my deploy"
    )
    db.commit()

    recent = community.list_recent_kudos(db, cohort=giver.cohort)
    assert len(recent) == 1
    assert recent[0].message == "Saved my deploy"
    assert community.kudos_received_count(
        db, cohort_id=giver.cohort_id, user_id=receiver.id
    ) == 1

    notes, _ = notifications.list_for_user(
        db, cohort_id=receiver.cohort_id, user=receiver.user
    )
    assert notes[0].notification_type == NotificationType.KUDOS_RECEIVED


def test_cannot_give_yourself_kudos(db, make_user):
    person = make_user("Solo")
    with pytest.raises(ValidationError) as exc:
        community.give_kudos(
            db, actor=person.membership, to_user_id=person.id, message="I am great"
        )
    assert exc.value.code == "KUDOS_SELF"


def test_cannot_give_kudos_across_cohorts(db, make_user, make_cohort):
    giver = make_user("Cohort A Giver")
    other = make_user("Cohort B Person", cohort=make_cohort("Cohort B"))
    with pytest.raises(NotFoundError):
        community.give_kudos(
            db, actor=giver.membership, to_user_id=other.id, message="hi there"
        )


# ---------------------------------------------------------------------------
# Check-ins
# ---------------------------------------------------------------------------


def test_check_in_posts_and_updates_current_project(db, make_user):
    person = make_user("Daily Builder")
    community.post_check_in(db, actor=person.membership, body="Wiring the invite flow")
    db.commit()

    feed = community.list_recent_check_ins(db, cohort=person.cohort)
    assert feed[0].body == "Wiring the invite flow"
    assert person.membership.current_project == "Wiring the invite flow"
    todays = community.todays_check_in(db, cohort_id=person.cohort_id, user_id=person.id)
    assert todays is not None
    assert todays.body == "Wiring the invite flow"


def test_blank_check_in_is_rejected(db, make_user):
    person = make_user("Blank Check")
    with pytest.raises(ValidationError):
        community.post_check_in(db, actor=person.membership, body="  ")


# ---------------------------------------------------------------------------
# Pulse / campfire
# ---------------------------------------------------------------------------


def test_pulse_grows_with_activity_and_is_cohort_scoped(db, make_user, make_channel, make_cohort):
    admin = make_user("Pulse Admin", admin=True)
    channel = make_channel(admin, "Pulse Channel")
    member = make_user("Pulse Member")

    # An empty cohort (nobody joined, nothing happened) is stone cold.
    empty = community.compute_pulse(db, cohort=make_cohort("Empty Cohort"))
    assert empty.score == 0
    assert empty.level == 0

    # Two members just joined, so there's already a spark before other activity.
    baseline = community.compute_pulse(db, cohort=admin.cohort)
    assert baseline.counts["new_members"] == 2

    # Generate a spread of activity.
    for i in range(6):
        messages.create_message(db, actor=admin.membership, channel=channel, body=f"msg {i}")
    community.give_kudos(db, actor=admin.membership, to_user_id=member.id, message="nice work")
    community.post_check_in(db, actor=member.membership, body="shipping things")
    task = tasks.create_task(db, creator=admin.membership, title="Ship it")
    tasks.update_task_status(db, actor=admin.membership, task=task, status=TaskStatus.IN_PROGRESS)
    tasks.update_task_status(db, actor=admin.membership, task=task, status=TaskStatus.DONE)
    db.commit()

    pulse = community.compute_pulse(db, cohort=admin.cohort)
    assert pulse.counts["messages"] == 6
    assert pulse.counts["kudos"] == 1
    assert pulse.counts["check_ins"] == 1
    assert pulse.counts["tasks_completed"] == 1
    assert pulse.score > baseline.score
    assert pulse.level >= 1
    assert pulse.label != "Cold"

    # A different cohort sees none of cohort A's messages/kudos/tasks.
    other_admin = make_user("Other Admin", admin=True, cohort=make_cohort("Quiet Cohort"))
    other_pulse = community.compute_pulse(db, cohort=other_admin.cohort)
    assert other_pulse.counts["messages"] == 0
    assert other_pulse.counts["kudos"] == 0
    assert other_pulse.counts["tasks_completed"] == 0


def test_pulse_only_counts_recent_activity(db, make_user, make_channel):
    admin = make_user("Window Admin", admin=True)
    channel = make_channel(admin, "Window Channel")
    old = messages.create_message(db, actor=admin.membership, channel=channel, body="ancient")
    # Backdate it beyond the 7-day window.
    old.created_at = utcnow() - dt.timedelta(days=30)
    db.commit()

    assert community.compute_pulse(db, cohort=admin.cohort, window_days=7).counts["messages"] == 0


# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------


def test_kudos_page_and_giving_via_web(client, db, make_user, sign_in):
    giver = make_user("Web Giver")
    receiver = make_user("Web Receiver")
    sign_in(giver)

    page = client.get("/kudos")
    assert page.status_code == 200
    assert "Give kudos" in page.text

    posted = client.post(
        "/kudos",
        data={"to_user_id": str(receiver.id), "message": "Great pairing session"},
        follow_redirects=False,
    )
    assert posted.status_code == 303
    assert "Great pairing session" in client.get("/kudos").text


def test_check_in_page_and_posting_via_web(client, db, make_user, sign_in):
    person = make_user("Web Checker")
    sign_in(person)

    assert client.get("/check-in").status_code == 200
    posted = client.post(
        "/check-in", data={"body": "Reviewing the pulse feature"}, follow_redirects=False
    )
    assert posted.status_code == 303
    assert "Reviewing the pulse feature" in client.get("/check-in").text


def test_home_shows_the_campfire(client, make_user, sign_in):
    person = make_user("Campfire Viewer")
    sign_in(person)
    home = client.get("/")
    assert home.status_code == 200
    assert "Cohort pulse" in home.text
    assert "online now" in home.text
