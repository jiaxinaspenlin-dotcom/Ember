"""Authorization boundaries. Nothing here may be enforced only in the browser."""

from __future__ import annotations

import pytest

from app.auth import permissions
from app.core.errors import NotFoundError, PermissionDeniedError
from app.services import channels, direct_messages, messages, tasks


def test_any_member_can_create_a_channel(client, make_user, sign_in):
    member = make_user("Regular Member")
    sign_in(member)
    response = client.post("/api/channels", json={"name": "Cohort General"})
    assert response.status_code == 201
    assert response.json()["slug"] == "cohort-general"


def test_channel_creator_can_manage_their_own_channel(db, make_user):
    creator = make_user("Channel Owner")
    channel = channels.create_channel(db, actor=creator, name="Owned Channel")
    db.commit()

    # The creator renames, archives and restores their own channel.
    channels.rename_channel(db, actor=creator, channel=channel, name="Renamed By Owner")
    channels.archive_channel(db, actor=creator, channel=channel)
    channels.restore_channel(db, actor=creator, channel=channel)
    db.commit()
    assert channel.name == "Renamed By Owner"
    assert channel.is_archived is False


def test_a_member_cannot_manage_someone_elses_channel(client, make_user, sign_in, db):
    owner = make_user("The Owner")
    channel = channels.create_channel(db, actor=owner, name="Owner Only")
    db.commit()

    meddler = make_user("Meddler")
    sign_in(meddler)
    assert client.patch(f"/api/channels/{channel.id}", json={"name": "Hijacked"}).status_code == 403
    assert client.post(f"/api/channels/{channel.id}/archive").status_code == 403
    assert client.post(f"/api/channels/{channel.id}/restore").status_code == 403


def test_admin_can_manage_any_channel(client, make_user, sign_in, db):
    owner = make_user("Some Member")
    channel = channels.create_channel(db, actor=owner, name="Community Space")
    db.commit()

    admin = make_user("Cohort Admin", admin=True)
    sign_in(admin)
    assert (
        client.patch(f"/api/channels/{channel.id}", json={"name": "Tidied Up"}).status_code == 200
    )
    assert client.post(f"/api/channels/{channel.id}/archive").status_code == 200


def test_non_member_cannot_post_in_a_channel(db, make_user, make_channel):
    admin = make_user("Channel Admin", admin=True)
    channel = make_channel(admin)
    outsider = make_user("Outsider")

    with pytest.raises(PermissionDeniedError) as exc:
        messages.create_message(db, actor=outsider, channel=channel, body="Let me in")
    assert exc.value.code == "NOT_A_CHANNEL_MEMBER"


def test_archived_channel_rejects_new_messages(db, make_user, make_channel):
    admin = make_user("Archiver", admin=True)
    channel = make_channel(admin)
    channels.archive_channel(db, actor=admin, channel=channel)
    db.commit()

    with pytest.raises(PermissionDeniedError) as exc:
        messages.create_message(db, actor=admin, channel=channel, body="Too late")
    assert exc.value.code == "CHANNEL_ARCHIVED"


def test_archived_channel_rejects_thread_replies(db, make_user, make_channel):
    admin = make_user("Archiver Two", admin=True)
    channel = make_channel(admin)
    parent = messages.create_message(db, actor=admin, channel=channel, body="Parent message")
    db.commit()

    channels.archive_channel(db, actor=admin, channel=channel)
    db.commit()

    with pytest.raises(PermissionDeniedError):
        messages.create_message(
            db, actor=admin, channel=channel, body="A reply", parent_message_id=parent.id
        )


def test_archived_channel_remains_readable(db, make_user, make_channel):
    admin = make_user("Reader Admin", admin=True)
    channel = make_channel(admin)
    messages.create_message(db, actor=admin, channel=channel, body="Historic message")
    db.commit()
    channels.archive_channel(db, actor=admin, channel=channel)
    db.commit()

    history = messages.list_messages(db, channel=channel)
    assert len(history) == 1
    assert history[0].body == "Historic message"


def test_direct_messages_are_invisible_to_non_participants(
    client, make_user, sign_in, db, fresh_client
):
    alice = make_user("Alice Participant")
    bob = make_user("Bob Participant")
    snooper = make_user("Snoopy Person")

    conversation = direct_messages.get_or_create_conversation(
        db, actor=alice, other_user_id=bob.id
    )
    messages.create_message(db, actor=alice, conversation=conversation, body="Private plan")
    db.commit()

    sign_in(snooper)
    listing = client.get(f"/api/direct-messages/{conversation.id}/messages")
    assert listing.status_code == 404  # existence itself is private
    assert listing.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"

    send = client.post(
        f"/api/direct-messages/{conversation.id}/messages", json={"body": "Hello?"}
    )
    assert send.status_code == 404


def test_participants_can_read_their_own_direct_messages(client, make_user, sign_in, db):
    alice = make_user("Alice Owner")
    bob = make_user("Bob Owner")
    conversation = direct_messages.get_or_create_conversation(
        db, actor=alice, other_user_id=bob.id
    )
    messages.create_message(db, actor=alice, conversation=conversation, body="Shared secret")
    db.commit()

    sign_in(bob)
    response = client.get(f"/api/direct-messages/{conversation.id}/messages")
    assert response.status_code == 200
    assert response.json()["items"][0]["body"] == "Shared secret"


def test_search_never_returns_other_peoples_direct_messages(client, make_user, sign_in, db):
    alice = make_user("Alice Searcher")
    bob = make_user("Bob Searcher")
    snooper = make_user("Curious Person")
    conversation = direct_messages.get_or_create_conversation(
        db, actor=alice, other_user_id=bob.id
    )
    messages.create_message(
        db, actor=alice, conversation=conversation, body="pineapple submarine"
    )
    db.commit()

    sign_in(snooper)
    results = client.get("/api/search", params={"q": "pineapple"}).json()["results"]
    assert results == []

    client.post("/api/auth/logout")
    sign_in(bob)
    mine = client.get("/api/search", params={"q": "pineapple"}).json()["results"]
    assert len(mine) == 1
    assert "pineapple" in mine[0]["excerpt"]


def test_notifications_are_private_to_their_recipient(client, make_user, sign_in, db, make_channel):
    admin = make_user("Notify Admin", admin=True)
    channel = make_channel(admin)
    target = make_user("Target Person")
    channels.join_channel(db, actor=target, channel=channel)
    db.commit()

    messages.create_message(
        db, actor=admin, channel=channel, body="Hey @Target-Person can you look?"
    )
    db.commit()

    other = make_user("Unrelated Person")
    sign_in(other)
    assert client.get("/api/notifications").json()["items"] == []

    client.post("/api/auth/logout")
    sign_in(target)
    mine = client.get("/api/notifications").json()["items"]
    assert len(mine) == 1
    assert mine[0]["notification_type"] == "mention"


def test_cannot_mark_someone_elses_notification_as_read(
    client, make_user, sign_in, db, make_channel
):
    admin = make_user("Sender Admin", admin=True)
    channel = make_channel(admin)
    target = make_user("Mention Target")
    channels.join_channel(db, actor=target, channel=channel)
    db.commit()
    messages.create_message(db, actor=admin, channel=channel, body="ping @Mention-Target")
    db.commit()

    sign_in(target)
    notification_id = client.get("/api/notifications").json()["items"][0]["id"]
    client.post("/api/auth/logout")

    attacker = make_user("Attacker Person")
    sign_in(attacker)
    response = client.post(f"/api/notifications/{notification_id}/read")
    assert response.status_code == 404


def test_only_the_author_may_edit_a_message(db, make_user, make_channel):
    admin = make_user("Msg Admin", admin=True)
    channel = make_channel(admin)
    other = make_user("Other Author")
    channels.join_channel(db, actor=other, channel=channel)
    db.commit()

    message = messages.create_message(db, actor=admin, channel=channel, body="Original text")
    db.commit()

    with pytest.raises(PermissionDeniedError):
        messages.edit_message(db, actor=other, message=message, body="Rewritten")


def test_admin_can_delete_any_message_but_author_edits_stay_restricted(
    db, make_user, make_channel
):
    admin = make_user("Moderator", admin=True)
    channel = make_channel(admin)
    member = make_user("Poster Person")
    channels.join_channel(db, actor=member, channel=channel)
    db.commit()

    message = messages.create_message(db, actor=member, channel=channel, body="Inappropriate")
    db.commit()

    messages.soft_delete_message(db, actor=admin, message=message)
    db.commit()
    assert message.deleted_at is not None

    with pytest.raises(PermissionDeniedError):
        messages.edit_message(db, actor=admin, message=message, body="Rewritten by admin")


def test_pinning_is_limited_to_the_channel_creator_and_admins(db, make_user, make_channel):
    admin = make_user("Pin Admin", admin=True)
    channel = make_channel(admin)  # admin owns this channel
    member = make_user("Pin Member")
    channels.join_channel(db, actor=member, channel=channel)
    db.commit()
    message = messages.create_message(db, actor=member, channel=channel, body="Useful link")
    db.commit()

    # A member who does not own the channel cannot pin, even their own message.
    with pytest.raises(PermissionDeniedError):
        messages.pin_message(db, actor=member, message=message)

    messages.pin_message(db, actor=admin, message=message)
    db.commit()
    assert message.is_pinned is True


def test_a_member_can_pin_in_a_channel_they_created(db, make_user):
    creator = make_user("Channel Creator")
    channel = channels.create_channel(db, actor=creator, name="My Space")
    other = make_user("Other Member")
    channels.join_channel(db, actor=other, channel=channel)
    message = messages.create_message(db, actor=other, channel=channel, body="Pin me")
    db.commit()

    # The creator curates their own channel...
    messages.pin_message(db, actor=creator, message=message)
    db.commit()
    assert message.is_pinned is True

    # ...but a plain member still cannot.
    another = make_user("Yet Another")
    channels.join_channel(db, actor=another, channel=channel)
    db.commit()
    second = messages.create_message(db, actor=another, channel=channel, body="Not pinnable")
    db.commit()
    with pytest.raises(PermissionDeniedError):
        messages.pin_message(db, actor=another, message=second)


def test_direct_messages_cannot_be_converted_into_cohort_items(db, make_user):
    alice = make_user("Convert Alice")
    bob = make_user("Convert Bob")
    conversation = direct_messages.get_or_create_conversation(
        db, actor=alice, other_user_id=bob.id
    )
    message = messages.create_message(
        db, actor=alice, conversation=conversation, body="Private idea"
    )
    db.commit()

    with pytest.raises(PermissionDeniedError) as exc:
        permissions.require_convertible_message(message)
    assert exc.value.code == "MESSAGE_NOT_CONVERTIBLE"


def test_unauthorized_task_status_update_is_rejected(client, make_user, sign_in, db):
    creator = make_user("Task Creator")
    assignee = make_user("Task Assignee")
    stranger = make_user("Task Stranger")
    task = tasks.create_task(
        db, creator=creator, title="Write the docs", assignee_id=assignee.id
    )
    db.commit()

    sign_in(stranger)
    response = client.put(f"/api/tasks/{task.id}/status", json={"status": "done"})
    assert response.status_code == 403

    client.post("/api/auth/logout")
    sign_in(assignee)
    allowed = client.put(f"/api/tasks/{task.id}/status", json={"status": "in_progress"})
    assert allowed.status_code == 200


def test_assignee_cannot_reassign_but_creator_can(db, make_user):
    creator = make_user("Owner Creator")
    assignee = make_user("Only Assignee")
    third = make_user("Third Person")
    task = tasks.create_task(db, creator=creator, title="Ship it", assignee_id=assignee.id)
    db.commit()

    with pytest.raises(PermissionDeniedError):
        tasks.assign_task(db, actor=assignee, task=task, assignee_id=third.id)

    tasks.assign_task(db, actor=creator, task=task, assignee_id=third.id)
    db.commit()
    assert task.assignee_id == third.id


def test_admin_endpoints_reject_members(client, make_user, sign_in):
    member = make_user("Plain Member")
    sign_in(member)
    assert client.get("/api/admin/stats").status_code == 403
    assert client.get("/api/admin/users").status_code == 403
    assert client.get("/api/admin/audit").status_code == 403


def test_admin_endpoints_allow_admins(client, make_user, sign_in):
    admin = make_user("Stats Admin", admin=True)
    sign_in(admin)
    stats = client.get("/api/admin/stats")
    assert stats.status_code == 200
    assert stats.json()["members"] >= 1


def test_role_change_requires_admin_and_is_audited(client, make_user, sign_in, db):
    admin = make_user("Promoting Admin", admin=True)
    member = make_user("Promotable Member")

    sign_in(member)
    assert (
        client.put(f"/api/admin/users/{admin.id}/role", json={"role": "member"}).status_code == 403
    )
    client.post("/api/auth/logout")

    sign_in(admin)
    response = client.put(f"/api/admin/users/{member.id}/role", json={"role": "admin"})
    assert response.status_code == 200
    db.refresh(member.membership)
    assert member.membership.is_admin is True

    from app.services import audit

    actions = [event.action.value for event in audit.recent_events(db)]
    assert "cohort.member_role_changed" in actions


def test_missing_conversation_raises_not_found(db, make_user):
    import uuid

    user = make_user("Lost Person")
    with pytest.raises(NotFoundError):
        direct_messages.get_conversation(db, uuid.uuid4(), actor=user)
