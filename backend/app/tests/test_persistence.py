"""Persistence guarantees: data survives sessions, restarts and new browsers."""

from __future__ import annotations

from sqlalchemy import func, select

from app.db.session import SessionLocal
from app.models.action import Decision, HelpRequest, Task
from app.models.channel import Channel
from app.models.engagement import AuditEvent, Notification
from app.models.message import Message, ReadReceipt
from app.models.user import User, UserSession
from app.services import channels, decisions, help_requests, messages, tasks


def test_data_written_in_one_session_is_visible_in_a_brand_new_session(db, make_user):
    admin = make_user("Persist Admin", admin=True)
    channel = channels.create_channel(db, actor=admin, name="Persistent Channel")
    message = messages.create_message(
        db, actor=admin, channel=channel, body="This must survive"
    )
    db.commit()

    # A completely separate connection and identity map.
    other = SessionLocal()
    try:
        stored = other.get(Message, message.id)
        assert stored is not None
        assert stored.body == "This must survive"
        stored_channel = other.get(Channel, channel.id)
        assert stored_channel is not None
        assert stored_channel.name == "Persistent Channel"
    finally:
        other.close()


def test_records_survive_logout_and_a_new_login(client, db, make_user, make_channel, fresh_client):
    admin = make_user("Round Trip Admin", admin=True)
    channel = make_channel(admin, "Round Trip")

    client.post(
        "/api/auth/login", json={"email": admin.email, "password": "correct-horse-9-battery"}
    )
    created = client.post(
        f"/api/messages/channel/{channel.id}", json={"body": "Written before logout"}
    )
    assert created.status_code == 201
    message_id = created.json()["id"]

    client.post("/api/auth/logout")
    assert client.get(f"/api/messages/{message_id}").status_code == 401

    # A different browser, a fresh login: the data is still there.
    other = fresh_client()
    other.post(
        "/api/auth/login", json={"email": admin.email, "password": "correct-horse-9-battery"}
    )
    fetched = other.get(f"/api/messages/{message_id}")
    assert fetched.status_code == 200
    assert fetched.json()["body"] == "Written before logout"


def test_session_rows_persist_across_sessions(db, make_user):
    from app.auth import sessions

    user = make_user("Session Persistence")
    session_row, raw = sessions.create_session(db, user)
    db.commit()

    other = SessionLocal()
    try:
        assert other.get(UserSession, session_row.id) is not None
        assert sessions.resolve_session(other, raw) is not None
    finally:
        other.close()


def test_read_receipts_persist(db, make_user, make_channel):
    admin = make_user("Receipt Admin", admin=True)
    channel = make_channel(admin)
    reader = make_user("Receipt Reader")
    channels.join_channel(db, actor=reader, channel=channel)
    messages.create_message(db, actor=admin, channel=channel, body="Read me")
    db.commit()

    messages.update_read_receipt(db, actor=reader, channel=channel)
    db.commit()

    other = SessionLocal()
    try:
        receipt = other.scalar(
            select(ReadReceipt).where(
                ReadReceipt.user_id == reader.id, ReadReceipt.channel_id == channel.id
            )
        )
        assert receipt is not None
        assert receipt.last_read_seq > 0
    finally:
        other.close()


def test_notifications_persist(db, make_user, make_channel):
    admin = make_user("Notify Admin", admin=True)
    channel = make_channel(admin)
    target = make_user("Notify Target")
    channels.join_channel(db, actor=target, channel=channel)
    db.commit()
    messages.create_message(db, actor=admin, channel=channel, body="ping @Notify-Target")
    db.commit()

    other = SessionLocal()
    try:
        stored = other.scalars(
            select(Notification).where(Notification.recipient_id == target.id)
        ).all()
        assert len(stored) == 1
    finally:
        other.close()


def test_actions_persist_with_their_state(db, make_user, make_channel):
    admin = make_user("Action Admin", admin=True)
    helper = make_user("Action Helper")
    request = help_requests.create_help_request(
        db, requester=admin, title="Persisted request", description="Body."
    )
    decision = decisions.create_decision(
        db, author=admin, title="Persisted decision", decision_text="Body."
    )
    task = tasks.create_task(
        db, creator=admin, title="Persisted task", assignee_id=helper.id
    )
    db.commit()
    help_requests.claim_help_request(db, actor=helper, help_request=request)
    db.commit()

    other = SessionLocal()
    try:
        stored_request = other.get(HelpRequest, request.id)
        stored_decision = other.get(Decision, decision.id)
        stored_task = other.get(Task, task.id)
        assert stored_request is not None and stored_request.status.value == "claimed"
        assert stored_decision is not None and stored_decision.status.value == "active"
        assert stored_task is not None and stored_task.assignee_id == helper.id
    finally:
        other.close()


def test_soft_deleted_messages_remain_in_the_database(db, make_user, make_channel):
    admin = make_user("Delete Admin", admin=True)
    channel = make_channel(admin)
    message = messages.create_message(db, actor=admin, channel=channel, body="Kept for audit")
    db.commit()
    messages.soft_delete_message(db, actor=admin, message=message)
    db.commit()

    other = SessionLocal()
    try:
        stored = other.get(Message, message.id)
        assert stored is not None
        assert stored.deleted_at is not None
    finally:
        other.close()


def test_audit_events_are_written_for_key_actions(db, make_user, make_channel):
    admin = make_user("Audited Admin", admin=True)
    channel = make_channel(admin, "Audited Channel")
    messages.create_message(db, actor=admin, channel=channel, body="Audited message")
    db.commit()

    other = SessionLocal()
    try:
        actions = {
            event.action.value for event in other.scalars(select(AuditEvent)).all()
        }
        assert {"user.registered", "channel.created", "message.created"} <= actions
    finally:
        other.close()


def test_audit_context_never_contains_message_bodies(db, make_user, make_channel):
    admin = make_user("Audit Privacy Admin", admin=True)
    channel = make_channel(admin)
    messages.create_message(
        db, actor=admin, channel=channel, body="a very distinctive phrase indeed"
    )
    db.commit()

    events = db.scalars(select(AuditEvent)).all()
    for event in events:
        assert "a very distinctive phrase indeed" not in str(event.context or {})


def test_failed_write_rolls_back_and_leaves_no_partial_state(db, make_user):
    """A constraint violation must not leave a half-written graph behind."""

    from sqlalchemy.exc import IntegrityError

    admin = make_user("Rollback Admin", admin=True)
    channels.create_channel(db, actor=admin, name="Rollback Channel")
    db.commit()
    before = db.scalar(select(func.count()).select_from(Channel))

    duplicate = Channel(slug="rollback-channel", name="Duplicate", created_by_id=admin.id)
    db.add(duplicate)
    try:
        db.commit()
        raise AssertionError("expected the unique slug constraint to fire")
    except IntegrityError:
        db.rollback()

    assert db.scalar(select(func.count()).select_from(Channel)) == before


def test_targeted_edit_does_not_recreate_related_rows(db, make_user, make_channel):
    """Editing a message must not delete and rebuild its reactions."""

    from app.core.enums import ReactionType
    from app.models.message import Reaction

    admin = make_user("Targeted Admin", admin=True)
    channel = make_channel(admin)
    reactor = make_user("Reactor Person")
    channels.join_channel(db, actor=reactor, channel=channel)
    message = messages.create_message(db, actor=admin, channel=channel, body="Before edit")
    db.commit()

    messages.add_reaction(
        db, actor=reactor, message=message, reaction_type=ReactionType.THUMBS_UP
    )
    db.commit()
    reaction_id = db.scalar(
        select(Reaction.id).where(Reaction.message_id == message.id)
    )

    messages.edit_message(db, actor=admin, message=message, body="After edit")
    db.commit()

    assert (
        db.scalar(select(Reaction.id).where(Reaction.message_id == message.id)) == reaction_id
    )


def test_user_count_reflects_only_real_registrations(db, make_user):
    assert db.scalar(select(func.count()).select_from(User)) == 0
    make_user("Only Real Person")
    assert db.scalar(select(func.count()).select_from(User)) == 1
