"""Help requests, decisions and tasks: state machines and notifications."""

from __future__ import annotations

import pytest

from app.core.enums import (
    DecisionStatus,
    HelpCategory,
    HelpRequestStatus,
    Priority,
    TaskStatus,
)
from app.core.errors import (
    InvalidStateTransitionError,
    PermissionDeniedError,
    ValidationError,
)
from app.services import channels, decisions, help_requests, messages, notifications, tasks


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


@pytest.fixture
def source_message(db, cohort):
    message = messages.create_message(
        db,
        actor=cohort["maya"],
        channel=cohort["channel"],
        body="Deploy keeps failing on the staging box",
    )
    db.commit()
    return message


# ---------------------------------------------------------------------------
# Help requests
# ---------------------------------------------------------------------------


def test_create_help_request_from_a_message_links_the_source(db, cohort, source_message):
    request = help_requests.create_help_request(
        db,
        requester=cohort["maya"],
        title="Staging deploy fails",
        description="Build succeeds locally but fails on staging.",
        category=HelpCategory.DEPLOYMENT,
        urgency=Priority.HIGH,
        source_message=source_message,
    )
    db.commit()

    assert request.status is HelpRequestStatus.OPEN
    assert request.original_message_id == source_message.id
    assert request.source_channel_id == cohort["channel"].id
    assert request.assigned_helper_id is None


def test_help_request_requires_a_real_title_and_description(db, cohort):
    with pytest.raises(ValidationError):
        help_requests.create_help_request(
            db, requester=cohort["maya"], title="Hi", description="Something"
        )
    with pytest.raises(ValidationError):
        help_requests.create_help_request(
            db, requester=cohort["maya"], title="Valid title here", description="   "
        )


def test_claim_resolve_flow_with_notifications(db, cohort):
    request = help_requests.create_help_request(
        db, requester=cohort["maya"], title="Need a reviewer", description="PR is ready."
    )
    db.commit()

    help_requests.claim_help_request(db, actor=cohort["sam"], help_request=request)
    db.commit()
    assert request.status is HelpRequestStatus.CLAIMED
    assert request.assigned_helper_id == cohort["sam"].id
    assert request.claimed_at is not None

    maya_notifications, _ = notifications.list_for_user(
        db, cohort_id=cohort["maya"].cohort_id, user=cohort["maya"]
    )
    assert any(n.notification_type.value == "help_request_claimed" for n in maya_notifications)

    help_requests.resolve_help_request(
        db, actor=cohort["sam"], help_request=request, resolution_note="Reviewed and merged."
    )
    db.commit()
    assert request.status.value == "resolved"
    assert request.resolved_at is not None
    assert request.resolution_note == "Reviewed and merged."

    maya_notifications, _ = notifications.list_for_user(
        db, cohort_id=cohort["maya"].cohort_id, user=cohort["maya"]
    )
    assert any(n.notification_type.value == "help_request_resolved" for n in maya_notifications)


def test_requester_cannot_claim_their_own_request(db, cohort):
    request = help_requests.create_help_request(
        db, requester=cohort["maya"], title="My own request", description="Please help."
    )
    db.commit()
    with pytest.raises(PermissionDeniedError) as exc:
        help_requests.claim_help_request(db, actor=cohort["maya"], help_request=request)
    assert exc.value.code == "CANNOT_CLAIM_OWN_REQUEST"


def test_unclaim_returns_the_request_to_the_queue(db, cohort):
    request = help_requests.create_help_request(
        db, requester=cohort["maya"], title="Pair on caching", description="Stuck on invalidation."
    )
    db.commit()
    help_requests.claim_help_request(db, actor=cohort["sam"], help_request=request)
    db.commit()

    help_requests.unclaim_help_request(db, actor=cohort["sam"], help_request=request)
    db.commit()
    assert request.status is HelpRequestStatus.OPEN
    assert request.assigned_helper_id is None
    assert request.claimed_at is None


def test_only_authorised_people_can_resolve(db, cohort, make_user):
    stranger = make_user("Random Stranger")
    request = help_requests.create_help_request(
        db, requester=cohort["maya"], title="Design review", description="Need eyes on this."
    )
    db.commit()

    with pytest.raises(PermissionDeniedError):
        help_requests.resolve_help_request(db, actor=stranger, help_request=request)

    # The requester may resolve their own request.
    help_requests.resolve_help_request(db, actor=cohort["maya"], help_request=request)
    db.commit()
    assert request.status is HelpRequestStatus.RESOLVED


def test_admin_can_resolve_any_request(db, cohort):
    request = help_requests.create_help_request(
        db, requester=cohort["maya"], title="Anything at all", description="Details."
    )
    db.commit()
    help_requests.resolve_help_request(db, actor=cohort["admin"], help_request=request)
    db.commit()
    assert request.status is HelpRequestStatus.RESOLVED


def test_reopen_clears_resolution_state(db, cohort):
    request = help_requests.create_help_request(
        db, requester=cohort["maya"], title="Came back again", description="Details."
    )
    db.commit()
    help_requests.claim_help_request(db, actor=cohort["sam"], help_request=request)
    help_requests.resolve_help_request(db, actor=cohort["sam"], help_request=request)
    db.commit()

    help_requests.reopen_help_request(db, actor=cohort["maya"], help_request=request)
    db.commit()
    assert request.status is HelpRequestStatus.OPEN
    assert request.resolved_at is None
    assert request.assigned_helper_id is None


def test_cancel_then_reopen(db, cohort):
    request = help_requests.create_help_request(
        db, requester=cohort["maya"], title="Never mind for now", description="Details."
    )
    db.commit()
    help_requests.cancel_help_request(db, actor=cohort["maya"], help_request=request)
    db.commit()
    assert request.status is HelpRequestStatus.CANCELLED

    help_requests.reopen_help_request(db, actor=cohort["maya"], help_request=request)
    db.commit()
    assert request.status.value == "open"


def test_resolved_request_cannot_be_claimed(db, cohort):
    request = help_requests.create_help_request(
        db, requester=cohort["maya"], title="Already handled", description="Details."
    )
    db.commit()
    help_requests.resolve_help_request(db, actor=cohort["maya"], help_request=request)
    db.commit()

    with pytest.raises(InvalidStateTransitionError) as exc:
        help_requests.claim_help_request(db, actor=cohort["sam"], help_request=request)
    assert exc.value.code == "HELP_REQUEST_INVALID_TRANSITION"


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (HelpRequestStatus.OPEN, HelpRequestStatus.CLAIMED, True),
        (HelpRequestStatus.OPEN, HelpRequestStatus.RESOLVED, True),
        (HelpRequestStatus.CLAIMED, HelpRequestStatus.OPEN, True),
        (HelpRequestStatus.RESOLVED, HelpRequestStatus.CLAIMED, False),
        (HelpRequestStatus.CANCELLED, HelpRequestStatus.RESOLVED, False),
        (HelpRequestStatus.RESOLVED, HelpRequestStatus.OPEN, True),
    ],
)
def test_help_request_transition_table(current, target, allowed):
    assert help_requests.can_transition(current, target) is allowed


def test_help_queue_filters(db, cohort):
    open_one = help_requests.create_help_request(
        db,
        requester=cohort["maya"],
        title="Coding question here",
        description="Details.",
        category=HelpCategory.CODING,
        urgency=Priority.URGENT,
    )
    claimed = help_requests.create_help_request(
        db,
        requester=cohort["maya"],
        title="Design question here",
        description="Details.",
        category=HelpCategory.DESIGN,
    )
    db.commit()
    help_requests.claim_help_request(db, actor=cohort["sam"], help_request=claimed)
    db.commit()

    unclaimed, total = help_requests.list_help_requests(
        db,
        cohort=cohort["sam"].cohort,
        user=cohort["sam"],
        filters=help_requests.HelpFilters(unclaimed=True),
    )
    assert [item.id for item in unclaimed] == [open_one.id]
    assert total == 1

    mine, _ = help_requests.list_help_requests(
        db,
        cohort=cohort["sam"].cohort,
        user=cohort["sam"],
        filters=help_requests.HelpFilters(assigned_to_me=True),
    )
    assert [item.id for item in mine] == [claimed.id]

    by_category, _ = help_requests.list_help_requests(
        db,
        cohort=cohort["maya"].cohort,
        user=cohort["maya"],
        filters=help_requests.HelpFilters(category=HelpCategory.CODING),
    )
    assert [item.id for item in by_category] == [open_one.id]

    by_urgency, _ = help_requests.list_help_requests(
        db,
        cohort=cohort["maya"].cohort,
        user=cohort["maya"],
        filters=help_requests.HelpFilters(urgency=Priority.URGENT),
    )
    assert [item.id for item in by_urgency] == [open_one.id]

    searched, _ = help_requests.list_help_requests(
        db,
        cohort=cohort["maya"].cohort,
        user=cohort["maya"],
        filters=help_requests.HelpFilters(query="design"),
    )
    assert [item.id for item in searched] == [claimed.id]


def test_feedback_requests_use_the_help_request_model(client, cohort, sign_in, source_message):
    sign_in(cohort["maya"])
    response = client.post(
        "/api/help-requests",
        json={
            "title": "Feedback on the onboarding copy",
            "description": "Would love a second opinion.",
            "category": "feedback",
            "source_message_id": str(source_message.id),
        },
    )
    assert response.status_code == 201
    assert response.json()["category"] == "feedback"


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------


def test_create_decision_from_a_message(db, cohort, source_message):
    decision = decisions.create_decision(
        db,
        author=cohort["maya"],
        title="Move staging to the new pipeline",
        decision_text="We will migrate staging first, production next week.",
        context="Current pipeline is flaky.",
        related_project="Platform",
        source_message=source_message,
    )
    db.commit()

    assert decision.status is DecisionStatus.ACTIVE
    assert decision.original_message_id == source_message.id
    assert decision.source_channel_id == cohort["channel"].id


def test_supersede_links_the_replacement_and_keeps_history(db, cohort):
    old = decisions.create_decision(
        db, author=cohort["maya"], title="Use REST for v1", decision_text="REST it is."
    )
    new = decisions.create_decision(
        db, author=cohort["maya"], title="Use GraphQL for v2", decision_text="GraphQL now."
    )
    db.commit()

    decisions.supersede_decision(db, actor=cohort["maya"], decision=old, replacement_id=new.id)
    db.commit()

    assert old.status is DecisionStatus.SUPERSEDED
    assert old.superseded_by_id == new.id
    assert old.superseded_at is not None
    # Nothing was deleted: both rows remain readable.
    assert decisions.get_decision(db, cohort["admin"].cohort, old.id) is not None
    assert (
        decisions.get_decision(db, cohort["admin"].cohort, new.id).status is DecisionStatus.ACTIVE
    )


def test_decision_cannot_supersede_itself(db, cohort):
    decision = decisions.create_decision(
        db, author=cohort["maya"], title="A standalone decision", decision_text="Text."
    )
    db.commit()
    with pytest.raises(ValidationError) as exc:
        decisions.supersede_decision(
            db, actor=cohort["maya"], decision=decision, replacement_id=decision.id
        )
    assert exc.value.code == "DECISION_SELF_SUPERSEDE"


def test_superseded_decision_cannot_be_superseded_again(db, cohort):
    first = decisions.create_decision(
        db, author=cohort["maya"], title="First decision here", decision_text="Text."
    )
    second = decisions.create_decision(
        db, author=cohort["maya"], title="Second decision here", decision_text="Text."
    )
    third = decisions.create_decision(
        db, author=cohort["maya"], title="Third decision here", decision_text="Text."
    )
    db.commit()
    decisions.supersede_decision(db, actor=cohort["maya"], decision=first, replacement_id=second.id)
    db.commit()

    with pytest.raises(InvalidStateTransitionError):
        decisions.supersede_decision(
            db, actor=cohort["maya"], decision=first, replacement_id=third.id
        )


def test_reverse_records_a_reason(db, cohort):
    decision = decisions.create_decision(
        db, author=cohort["maya"], title="A reversible decision", decision_text="Text."
    )
    db.commit()
    decisions.reverse_decision(
        db, actor=cohort["maya"], decision=decision, reason="Requirements changed."
    )
    db.commit()

    assert decision.status is DecisionStatus.REVERSED
    assert decision.reversal_reason == "Requirements changed."
    assert decision.reversed_by_id == cohort["maya"].id


def test_only_author_or_admin_can_change_a_decision(db, cohort, make_user):
    stranger = make_user("Unrelated Person")
    decision = decisions.create_decision(
        db, author=cohort["maya"], title="Protected decision here", decision_text="Text."
    )
    db.commit()

    with pytest.raises(PermissionDeniedError):
        decisions.reverse_decision(db, actor=stranger, decision=decision)

    decisions.reverse_decision(db, actor=cohort["admin"], decision=decision)
    db.commit()
    assert decision.status is DecisionStatus.REVERSED


def test_decision_author_is_notified_when_someone_else_changes_it(db, cohort):
    decision = decisions.create_decision(
        db, author=cohort["maya"], title="Notify on change here", decision_text="Text."
    )
    db.commit()
    decisions.reverse_decision(db, actor=cohort["admin"], decision=decision)
    db.commit()

    maya_notifications, _ = notifications.list_for_user(
        db, cohort_id=cohort["maya"].cohort_id, user=cohort["maya"]
    )
    assert any(n.notification_type.value == "decision_changed" for n in maya_notifications)


def test_decision_log_filters(db, cohort):
    decisions.create_decision(
        db,
        author=cohort["maya"],
        title="Adopt trunk based development",
        decision_text="Short-lived branches only.",
        related_project="Platform",
    )
    decisions.create_decision(
        db,
        author=cohort["sam"],
        title="Choose Postgres for storage",
        decision_text="Relational fits our data.",
        related_project="Data",
    )
    db.commit()

    by_author, _ = decisions.list_decisions(
        db,
        cohort=cohort["admin"].cohort,
        filters=decisions.DecisionFilters(author_id=cohort["sam"].id),
    )
    assert len(by_author) == 1
    assert by_author[0].title.startswith("Choose Postgres")

    by_project, _ = decisions.list_decisions(
        db,
        cohort=cohort["admin"].cohort,
        filters=decisions.DecisionFilters(related_project="platform"),
    )
    assert len(by_project) == 1

    searched, _ = decisions.list_decisions(
        db, cohort=cohort["admin"].cohort, filters=decisions.DecisionFilters(query="postgres")
    )
    assert len(searched) == 1

    _active, total = decisions.list_decisions(
        db,
        cohort=cohort["admin"].cohort,
        filters=decisions.DecisionFilters(status=DecisionStatus.ACTIVE),
    )
    assert total == 2


@pytest.mark.parametrize(
    ("current", "target", "allowed"),
    [
        (DecisionStatus.ACTIVE, DecisionStatus.SUPERSEDED, True),
        (DecisionStatus.ACTIVE, DecisionStatus.REVERSED, True),
        (DecisionStatus.SUPERSEDED, DecisionStatus.ACTIVE, False),
        (DecisionStatus.REVERSED, DecisionStatus.SUPERSEDED, False),
    ],
)
def test_decision_transition_table(current, target, allowed):
    assert decisions.can_transition(current, target) is allowed


# ---------------------------------------------------------------------------
# Tasks
# ---------------------------------------------------------------------------


def test_create_task_from_a_message_and_notify_the_assignee(db, cohort, source_message):
    task = tasks.create_task(
        db,
        creator=cohort["maya"],
        title="Fix the staging deploy",
        description="Investigate the failing step.",
        assignee_id=cohort["sam"].id,
        priority=Priority.HIGH,
        source_message=source_message,
    )
    db.commit()

    assert task.status is TaskStatus.TODO
    assert task.assignee_id == cohort["sam"].id
    assert task.source_message_id == source_message.id
    assert task.source_channel_id == cohort["channel"].id

    sam_notifications, _ = notifications.list_for_user(
        db, cohort_id=cohort["sam"].cohort_id, user=cohort["sam"]
    )
    assert any(n.notification_type.value == "task_assigned" for n in sam_notifications)


def test_task_status_updates_set_and_clear_completed_at(db, cohort):
    task = tasks.create_task(
        db, creator=cohort["maya"], title="Write the readme", assignee_id=cohort["sam"].id
    )
    db.commit()

    tasks.update_task_status(db, actor=cohort["sam"], task=task, status=TaskStatus.IN_PROGRESS)
    db.commit()
    assert task.completed_at is None

    tasks.update_task_status(db, actor=cohort["sam"], task=task, status=TaskStatus.DONE)
    db.commit()
    assert task.status is TaskStatus.DONE
    assert task.completed_at is not None

    tasks.update_task_status(db, actor=cohort["sam"], task=task, status=TaskStatus.TODO)
    db.commit()
    assert task.completed_at is None


def test_reassignment_notifies_the_new_assignee(db, cohort):
    task = tasks.create_task(db, creator=cohort["admin"], title="Prepare the demo")
    db.commit()
    tasks.assign_task(db, actor=cohort["admin"], task=task, assignee_id=cohort["maya"].id)
    db.commit()

    maya_notifications, _ = notifications.list_for_user(
        db, cohort_id=cohort["maya"].cohort_id, user=cohort["maya"]
    )
    assert any(n.notification_type.value == "task_assigned" for n in maya_notifications)

    tasks.assign_task(db, actor=cohort["admin"], task=task, assignee_id=None)
    db.commit()
    assert task.assignee_id is None


def test_task_filters(db, cohort):
    mine = tasks.create_task(
        db, creator=cohort["maya"], title="Assigned to Sam", assignee_id=cohort["sam"].id
    )
    unassigned = tasks.create_task(db, creator=cohort["maya"], title="Nobody owns this")
    db.commit()

    assigned, _ = tasks.list_tasks(
        db,
        cohort=cohort["sam"].cohort,
        user=cohort["sam"],
        filters=tasks.TaskFilters(assigned_to_me=True),
    )
    assert [task.id for task in assigned] == [mine.id]

    free, _ = tasks.list_tasks(
        db,
        cohort=cohort["maya"].cohort,
        user=cohort["maya"],
        filters=tasks.TaskFilters(unassigned=True),
    )
    assert [task.id for task in free] == [unassigned.id]

    created, _ = tasks.list_tasks(
        db,
        cohort=cohort["maya"].cohort,
        user=cohort["maya"],
        filters=tasks.TaskFilters(created_by_me=True),
    )
    assert len(created) == 2

    searched, _ = tasks.list_tasks(
        db,
        cohort=cohort["maya"].cohort,
        user=cohort["maya"],
        filters=tasks.TaskFilters(query="nobody"),
    )
    assert [task.id for task in searched] == [unassigned.id]


def test_open_task_count_excludes_done(db, cohort):
    first = tasks.create_task(
        db, creator=cohort["maya"], title="Open task one", assignee_id=cohort["sam"].id
    )
    tasks.create_task(
        db, creator=cohort["maya"], title="Open task two", assignee_id=cohort["sam"].id
    )
    db.commit()
    assert (
        tasks.count_open_for_user(db, cohort_id=cohort["sam"].cohort_id, user_id=cohort["sam"].id)
        == 2
    )

    tasks.update_task_status(db, actor=cohort["sam"], task=first, status=TaskStatus.DONE)
    db.commit()
    assert (
        tasks.count_open_for_user(db, cohort_id=cohort["sam"].cohort_id, user_id=cohort["sam"].id)
        == 1
    )


def test_task_title_is_validated(db, cohort):
    with pytest.raises(ValidationError):
        tasks.create_task(db, creator=cohort["maya"], title="ab")


# ---------------------------------------------------------------------------
# Announcements
# ---------------------------------------------------------------------------


def test_announcement_creation_notifies_everyone_else(db, cohort):
    from app.services import announcements

    announcements.create_announcement(
        db,
        author=cohort["admin"],
        title="Demo day is on Friday",
        body="Bring a two minute update.",
        priority=Priority.HIGH,
    )
    db.commit()

    for member in (cohort["maya"], cohort["sam"]):
        member_notifications, _ = notifications.list_for_user(
            db, cohort_id=member.cohort_id, user=member
        )
        assert any(n.notification_type.value == "announcement" for n in member_notifications)

    author_notifications, _ = notifications.list_for_user(
        db, cohort_id=cohort["admin"].cohort_id, user=cohort["admin"]
    )
    assert author_notifications == []


def test_members_cannot_create_announcements(client, cohort, sign_in):
    sign_in(cohort["maya"])
    response = client.post(
        "/api/announcements", json={"title": "Unauthorized post", "body": "Nope."}
    )
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def test_mark_notification_read_and_read_all(db, cohort):
    from app.services import announcements

    announcements.create_announcement(
        db, author=cohort["admin"], title="First announcement", body="Body."
    )
    announcements.create_announcement(
        db, author=cohort["admin"], title="Second announcement", body="Body."
    )
    db.commit()

    assert (
        notifications.unread_count(
            db, cohort_id=cohort["maya"].cohort_id, user_id=cohort["maya"].id
        )
        == 2
    )

    items, _ = notifications.list_for_user(
        db, cohort_id=cohort["maya"].cohort_id, user=cohort["maya"]
    )
    notifications.mark_notification_read(db, user=cohort["maya"], notification_id=items[0].id)
    db.commit()
    assert (
        notifications.unread_count(
            db, cohort_id=cohort["maya"].cohort_id, user_id=cohort["maya"].id
        )
        == 1
    )

    notifications.mark_all_read(db, cohort_id=cohort["maya"].cohort_id, user=cohort["maya"])
    db.commit()
    assert (
        notifications.unread_count(
            db, cohort_id=cohort["maya"].cohort_id, user_id=cohort["maya"].id
        )
        == 0
    )
