"""Channels, messages, threads, reactions, mentions, pins, unread counts, polling."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.enums import ReactionType
from app.core.errors import ConflictError, ValidationError
from app.models.message import Mention, Message, Reaction
from app.services import channels, direct_messages, messages, notifications


@pytest.fixture
def cohort(db, make_user, make_channel):
    """A small real cohort: one admin, two members, one channel they all joined."""

    admin = make_user("Ada Admin", admin=True)
    maya = make_user("Maya Chen")
    sam = make_user("Sam Okoro")
    channel = make_channel(admin, "Build Log")
    for member in (maya, sam):
        channels.join_channel(db, actor=member, channel=channel)
    db.commit()
    return {"admin": admin, "maya": maya, "sam": sam, "channel": channel}


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------


def test_channel_creation_slugifies_and_adds_the_creator(db, make_user):
    admin = make_user("Slug Admin", admin=True)
    channel = channels.create_channel(db, actor=admin, name="  Product   Feedback! ")
    db.commit()
    assert channel.slug == "product-feedback"
    assert channels.member_count(db, channel.id) == 1


def test_duplicate_channel_slug_is_rejected(db, make_user):
    admin = make_user("Dup Admin", admin=True)
    channels.create_channel(db, actor=admin, name="General")
    db.commit()
    with pytest.raises(ConflictError):
        channels.create_channel(db, actor=admin, name="general")


def test_rename_keeps_the_slug_stable(db, make_user, make_channel):
    admin = make_user("Rename Admin", admin=True)
    channel = make_channel(admin, "Old Name")
    original_slug = channel.slug
    channels.rename_channel(db, actor=admin, channel=channel, name="Brand New Name")
    db.commit()
    assert channel.name == "Brand New Name"
    assert channel.slug == original_slug  # links never break


def test_archive_and_restore_round_trip(db, make_user, make_channel):
    admin = make_user("Archive Admin", admin=True)
    channel = make_channel(admin)

    channels.archive_channel(db, actor=admin, channel=channel)
    db.commit()
    assert channel.is_archived is True
    assert channel.archived_at is not None

    channels.restore_channel(db, actor=admin, channel=channel)
    db.commit()
    assert channel.is_archived is False
    assert channel.archived_at is None


def test_join_and_leave_are_idempotent_and_targeted(db, make_user, make_channel):
    admin = make_user("Membership Admin", admin=True)
    channel = make_channel(admin)
    member = make_user("Joiner Person")

    channels.join_channel(db, actor=member, channel=channel)
    channels.join_channel(db, actor=member, channel=channel)
    db.commit()
    assert channels.member_count(db, channel.id) == 2

    channels.leave_channel(db, actor=member, channel=channel)
    db.commit()
    assert channels.member_count(db, channel.id) == 1


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def test_message_persists_with_a_monotonic_sequence(db, cohort):
    first = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="First"
    )
    second = messages.create_message(
        db, actor=cohort["sam"], channel=cohort["channel"], body="Second"
    )
    db.commit()
    assert second.seq > first.seq
    assert db.get(Message, first.id).body == "First"


def test_empty_message_is_rejected(db, cohort):
    with pytest.raises(ValidationError):
        messages.create_message(db, actor=cohort["maya"], channel=cohort["channel"], body="   ")


def test_message_must_have_exactly_one_destination(db, cohort):
    with pytest.raises(ValidationError) as exc:
        messages.create_message(db, actor=cohort["maya"], body="Nowhere")
    assert exc.value.code == "INVALID_MESSAGE_DESTINATION"


def test_edit_marks_edited_at_and_keeps_the_row(db, cohort):
    message = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Typo here"
    )
    db.commit()
    messages.edit_message(db, actor=cohort["maya"], message=message, body="Fixed now")
    db.commit()

    stored = db.get(Message, message.id)
    assert stored.body == "Fixed now"
    assert stored.edited_at is not None


def test_soft_delete_keeps_the_row_for_audit(db, cohort):
    message = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Regrettable"
    )
    db.commit()
    messages.soft_delete_message(db, actor=cohort["maya"], message=message)
    db.commit()

    stored = db.get(Message, message.id)
    assert stored is not None
    assert stored.deleted_at is not None
    assert stored.body == "Regrettable"  # retained; never shown to users


def test_pagination_returns_pages_in_chronological_order(db, cohort):
    created = [
        messages.create_message(
            db, actor=cohort["maya"], channel=cohort["channel"], body=f"Message {index}"
        )
        for index in range(12)
    ]
    db.commit()

    newest = messages.list_messages(db, channel=cohort["channel"], limit=5)
    assert [m.body for m in newest] == [f"Message {i}" for i in range(7, 12)]

    older = messages.list_messages(db, channel=cohort["channel"], before_seq=newest[0].seq, limit=5)
    assert [m.body for m in older] == [f"Message {i}" for i in range(2, 7)]
    assert messages.has_older_messages(db, channel=cohort["channel"], oldest_seq=older[0].seq)
    assert created[0].seq < newest[0].seq


# ---------------------------------------------------------------------------
# Polling
# ---------------------------------------------------------------------------


def test_polling_returns_only_newer_messages(db, cohort):
    first = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Old news"
    )
    db.commit()

    assert messages.list_new_messages(db, channel=cohort["channel"], after_seq=first.seq) == []

    second = messages.create_message(
        db, actor=cohort["sam"], channel=cohort["channel"], body="Fresh news"
    )
    db.commit()

    new = messages.list_new_messages(db, channel=cohort["channel"], after_seq=first.seq)
    assert [m.id for m in new] == [second.id]


def test_polling_endpoint_does_not_replay_history(client, db, cohort, sign_in):
    for index in range(5):
        messages.create_message(
            db, actor=cohort["maya"], channel=cohort["channel"], body=f"Line {index}"
        )
    db.commit()
    latest = messages.latest_seq(db, channel=cohort["channel"])

    sign_in(cohort["sam"])
    response = client.get(
        f"/api/messages/channel/{cohort['channel'].id}/new", params={"after_seq": latest}
    )
    assert response.status_code == 200
    assert response.json()["count"] == 0
    assert response.json()["items"] == []


def test_polling_excludes_thread_replies_from_the_main_stream(db, cohort):
    parent = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Parent"
    )
    db.commit()
    messages.create_message(
        db,
        actor=cohort["sam"],
        channel=cohort["channel"],
        body="Reply in thread",
        parent_message_id=parent.id,
    )
    db.commit()

    stream = messages.list_new_messages(db, channel=cohort["channel"], after_seq=parent.seq)
    assert stream == []


# ---------------------------------------------------------------------------
# Threads
# ---------------------------------------------------------------------------


def test_thread_replies_load_only_when_requested(db, cohort):
    parent = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Design question"
    )
    db.commit()
    for index in range(3):
        messages.create_message(
            db,
            actor=cohort["sam"],
            channel=cohort["channel"],
            body=f"Answer {index}",
            parent_message_id=parent.id,
        )
    db.commit()
    db.refresh(parent)

    assert parent.reply_count == 3
    assert parent.last_reply_at is not None

    top_level = messages.list_messages(db, channel=cohort["channel"])
    assert len(top_level) == 1

    replies = messages.list_thread_replies(db, parent=parent)
    assert [r.body for r in replies] == ["Answer 0", "Answer 1", "Answer 2"]

    participants = {p.id for p in messages.thread_participants(db, parent=parent)}
    assert participants == {cohort["maya"].id, cohort["sam"].id}


def test_threads_cannot_nest(db, cohort):
    parent = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Root"
    )
    db.commit()
    reply = messages.create_message(
        db,
        actor=cohort["sam"],
        channel=cohort["channel"],
        body="Reply",
        parent_message_id=parent.id,
    )
    db.commit()

    with pytest.raises(ValidationError) as exc:
        messages.create_message(
            db,
            actor=cohort["maya"],
            channel=cohort["channel"],
            body="Reply to a reply",
            parent_message_id=reply.id,
        )
    assert exc.value.code == "NESTED_THREAD_NOT_ALLOWED"


def test_thread_reply_notifies_earlier_participants(db, cohort):
    parent = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Root question"
    )
    db.commit()
    messages.create_message(
        db,
        actor=cohort["sam"],
        channel=cohort["channel"],
        body="Here is an answer",
        parent_message_id=parent.id,
    )
    db.commit()

    maya_notifications, _ = notifications.list_for_user(
        db, cohort_id=cohort["maya"].cohort_id, user=cohort["maya"]
    )
    assert any(n.notification_type.value == "thread_reply" for n in maya_notifications)


# ---------------------------------------------------------------------------
# Reactions
# ---------------------------------------------------------------------------


def test_reactions_are_unique_per_user_type_and_message(db, cohort):
    message = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Shipped it"
    )
    db.commit()

    messages.add_reaction(
        db, actor=cohort["sam"], message=message, reaction_type=ReactionType.CELEBRATION
    )
    messages.add_reaction(
        db, actor=cohort["sam"], message=message, reaction_type=ReactionType.CELEBRATION
    )
    db.commit()

    stored = db.scalars(select(Reaction).where(Reaction.message_id == message.id)).all()
    assert len(stored) == 1


def test_database_rejects_a_duplicate_reaction_row(db, cohort):
    from sqlalchemy.exc import IntegrityError

    message = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Constraint check"
    )
    db.commit()
    db.add(
        Reaction(message_id=message.id, user_id=cohort["sam"].id, reaction_type=ReactionType.HEART)
    )
    db.commit()
    db.add(
        Reaction(message_id=message.id, user_id=cohort["sam"].id, reaction_type=ReactionType.HEART)
    )
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_reaction_toggle_and_summary(db, cohort):
    message = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Toggle me"
    )
    db.commit()

    assert (
        messages.toggle_reaction(
            db, actor=cohort["sam"], message=message, reaction_type=ReactionType.THUMBS_UP
        )
        is True
    )
    db.commit()
    db.refresh(message)

    summary = messages.summarize_reactions(message, viewer_id=cohort["sam"].id)
    assert summary[0].count == 1
    assert summary[0].reacted is True
    assert summary[0].participants == ["Sam Okoro"]

    seen_by_other = messages.summarize_reactions(message, viewer_id=cohort["maya"].id)
    assert seen_by_other[0].reacted is False

    assert (
        messages.toggle_reaction(
            db, actor=cohort["sam"], message=message, reaction_type=ReactionType.THUMBS_UP
        )
        is False
    )
    db.commit()
    db.refresh(message)
    assert messages.summarize_reactions(message, viewer_id=cohort["sam"].id) == []


def test_cannot_react_to_a_deleted_message(db, cohort):
    message = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Going away"
    )
    db.commit()
    messages.soft_delete_message(db, actor=cohort["maya"], message=message)
    db.commit()

    with pytest.raises(ConflictError):
        messages.add_reaction(
            db, actor=cohort["sam"], message=message, reaction_type=ReactionType.EYES
        )


# ---------------------------------------------------------------------------
# Mentions
# ---------------------------------------------------------------------------


def test_user_mention_creates_a_notification_for_that_user_only(db, cohort):
    message = messages.create_message(
        db,
        actor=cohort["maya"],
        channel=cohort["channel"],
        body="@Sam-Okoro could you take a look?",
    )
    db.commit()

    mentions_rows = db.scalars(select(Mention).where(Mention.message_id == message.id)).all()
    assert len(mentions_rows) == 1
    assert mentions_rows[0].mentioned_user_id == cohort["sam"].id

    sam_notifications, _ = notifications.list_for_user(
        db, cohort_id=cohort["sam"].cohort_id, user=cohort["sam"]
    )
    assert len(sam_notifications) == 1
    assert sam_notifications[0].notification_type.value == "mention"

    admin_notifications, _ = notifications.list_for_user(
        db, cohort_id=cohort["admin"].cohort_id, user=cohort["admin"]
    )
    assert admin_notifications == []


def test_channel_mention_notifies_every_channel_member_except_the_author(db, cohort):
    messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="@channel standup in 5"
    )
    db.commit()

    for member in (cohort["sam"], cohort["admin"]):
        member_notifications, _ = notifications.list_for_user(
            db, cohort_id=member.cohort_id, user=member
        )
        assert len(member_notifications) == 1

    author_notifications, _ = notifications.list_for_user(
        db, cohort_id=cohort["maya"].cohort_id, user=cohort["maya"]
    )
    assert author_notifications == []


def test_admins_mention_reaches_administrators(db, cohort):
    messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="@admins please review"
    )
    db.commit()
    admin_notifications, _ = notifications.list_for_user(
        db, cohort_id=cohort["admin"].cohort_id, user=cohort["admin"]
    )
    assert len(admin_notifications) == 1


def test_mentions_never_reach_users_without_access(db, make_user, cohort):
    outsider = make_user("Outside Person")
    messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="@Outside-Person hello"
    )
    db.commit()

    outsider_notifications, _ = notifications.list_for_user(
        db, cohort_id=outsider.cohort_id, user=outsider
    )
    assert outsider_notifications == []


def test_mention_parsing_is_pure_and_testable():
    from app.services.mentions import extract_handles, normalize_handle

    handles = extract_handles("Hi @Maya-Chen and @sam.okoro — cc @channel, email a@b.com")
    assert handles == ["Maya-Chen", "sam.okoro", "channel"]
    assert extract_handles("no mentions here") == []
    assert normalize_handle("Maya-Chen") == normalize_handle("maya chen") == "mayachen"


# ---------------------------------------------------------------------------
# Pins
# ---------------------------------------------------------------------------


def test_pin_and_unpin(db, cohort):
    message = messages.create_message(
        db, actor=cohort["maya"], channel=cohort["channel"], body="Team handbook link"
    )
    db.commit()

    messages.pin_message(db, actor=cohort["admin"], message=message)
    db.commit()
    assert [m.id for m in messages.list_pinned(db, channel=cohort["channel"])] == [message.id]

    messages.unpin_message(db, actor=cohort["admin"], message=message)
    db.commit()
    assert messages.list_pinned(db, channel=cohort["channel"]) == []


# ---------------------------------------------------------------------------
# Unread counts and read receipts
# ---------------------------------------------------------------------------


def test_unread_counts_are_computed_in_sql_and_ignore_your_own_messages(db, cohort):
    messages.create_message(db, actor=cohort["maya"], channel=cohort["channel"], body="One")
    messages.create_message(db, actor=cohort["maya"], channel=cohort["channel"], body="Two")
    db.commit()

    assert (
        messages.unread_count_for_channel(
            db, user_id=cohort["sam"].id, channel_id=cohort["channel"].id
        )
        == 2
    )
    # The author has already read their own messages.
    assert (
        messages.unread_count_for_channel(
            db, user_id=cohort["maya"].id, channel_id=cohort["channel"].id
        )
        == 0
    )


def test_read_receipt_clears_the_unread_count(db, cohort):
    messages.create_message(db, actor=cohort["maya"], channel=cohort["channel"], body="Hello")
    db.commit()

    messages.update_read_receipt(db, actor=cohort["sam"], channel=cohort["channel"])
    db.commit()
    assert (
        messages.unread_count_for_channel(
            db, user_id=cohort["sam"].id, channel_id=cohort["channel"].id
        )
        == 0
    )

    messages.create_message(db, actor=cohort["maya"], channel=cohort["channel"], body="Again")
    db.commit()
    assert (
        messages.unread_count_for_channel(
            db, user_id=cohort["sam"].id, channel_id=cohort["channel"].id
        )
        == 1
    )


def test_unread_counts_survive_a_new_session_on_another_device(
    client, db, cohort, sign_in, fresh_client
):
    messages.create_message(db, actor=cohort["maya"], channel=cohort["channel"], body="Ping")
    db.commit()

    sign_in(cohort["sam"])
    first_device = client.get(f"/api/messages/channel/{cohort['channel'].id}/unread").json()
    assert first_device["unread_count"] == 1

    second = fresh_client()
    second.post(
        "/api/auth/login",
        json={"email": cohort["sam"].email, "password": "correct-horse-9-battery"},
    )
    second_device = second.get(f"/api/messages/channel/{cohort['channel'].id}/unread").json()
    assert second_device["unread_count"] == 1


def test_total_unread_spans_channels_and_direct_messages(db, cohort):
    messages.create_message(db, actor=cohort["maya"], channel=cohort["channel"], body="Channel")
    conversation = direct_messages.get_or_create_conversation(
        db, actor=cohort["maya"], other_user_id=cohort["sam"].id
    )
    messages.create_message(db, actor=cohort["maya"], conversation=conversation, body="Direct")
    db.commit()

    assert (
        messages.total_unread(db, cohort_id=cohort["sam"].cohort_id, user_id=cohort["sam"].id) == 2
    )


def test_channel_listing_includes_per_user_unread_counts(db, cohort):
    messages.create_message(db, actor=cohort["maya"], channel=cohort["channel"], body="Unread")
    db.commit()

    items, total = channels.list_channels(db, cohort=cohort["sam"].cohort, user=cohort["sam"])
    assert total == 1
    assert items[0].unread_count == 1
    assert items[0].is_member is True

    author_items, _ = channels.list_channels(db, cohort=cohort["maya"].cohort, user=cohort["maya"])
    assert author_items[0].unread_count == 0


# ---------------------------------------------------------------------------
# Direct messages
# ---------------------------------------------------------------------------


def test_direct_conversation_is_unique_per_pair(db, cohort):
    first = direct_messages.get_or_create_conversation(
        db, actor=cohort["maya"], other_user_id=cohort["sam"].id
    )
    db.commit()
    second = direct_messages.get_or_create_conversation(
        db, actor=cohort["sam"], other_user_id=cohort["maya"].id
    )
    db.commit()
    assert first.id == second.id


def test_cannot_open_a_conversation_with_yourself(db, cohort):
    with pytest.raises(ValidationError):
        direct_messages.get_or_create_conversation(
            db, actor=cohort["maya"], other_user_id=cohort["maya"].id
        )


def test_direct_messages_persist_and_list_for_both_participants(db, cohort):
    conversation = direct_messages.get_or_create_conversation(
        db, actor=cohort["maya"], other_user_id=cohort["sam"].id
    )
    messages.create_message(
        db, actor=cohort["maya"], conversation=conversation, body="Can we pair tomorrow?"
    )
    db.commit()

    for member in (cohort["maya"], cohort["sam"]):
        items, total = direct_messages.list_conversations(db, cohort=member.cohort, user=member)
        assert total == 1
        last = items[0].last_message
        assert last is not None
        assert last.body == "Can we pair tomorrow?"

    sam_items, _ = direct_messages.list_conversations(
        db, cohort=cohort["sam"].cohort, user=cohort["sam"]
    )
    assert sam_items[0].unread_count == 1
    assert sam_items[0].other_member.id == cohort["maya"].id
