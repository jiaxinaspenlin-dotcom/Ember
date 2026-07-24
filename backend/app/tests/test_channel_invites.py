"""Per-channel admins, invitations, member removal and invite links."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.core.errors import NotFoundError, PermissionDeniedError
from app.models.channel import Channel, ChannelMember
from app.services import channels, notifications


def _is_member(db, channel, user) -> bool:  # type: ignore[no-untyped-def]
    return (
        db.scalar(
            select(ChannelMember).where(
                ChannelMember.channel_id == channel.id, ChannelMember.user_id == user.id
            )
        )
        is not None
    )


# ---------------------------------------------------------------------------
# Direct invites
# ---------------------------------------------------------------------------


def test_channel_admin_can_invite_a_member_and_they_are_notified(db, make_user):
    creator = make_user("Channel Owner")
    channel = channels.create_channel(db, actor=creator, name="Launch Team")
    invitee = make_user("Invited Person")
    db.commit()

    channels.invite_member(db, actor=creator, channel=channel, invitee=invitee)
    db.commit()

    assert _is_member(db, channel, invitee)
    items, _ = notifications.list_for_user(db, cohort_id=invitee.cohort_id, user=invitee)
    assert any(n.notification_type.value == "channel_invite" for n in items)


def test_inviting_an_existing_member_is_idempotent(db, make_user):
    creator = make_user("Owner Two")
    channel = channels.create_channel(db, actor=creator, name="Already In")
    invitee = make_user("Repeat Invite")
    db.commit()

    channels.invite_member(db, actor=creator, channel=channel, invitee=invitee)
    channels.invite_member(db, actor=creator, channel=channel, invitee=invitee)
    db.commit()

    memberships = db.scalars(
        select(ChannelMember).where(
            ChannelMember.channel_id == channel.id, ChannelMember.user_id == invitee.id
        )
    ).all()
    assert len(memberships) == 1


def test_a_plain_member_cannot_invite_to_someone_elses_channel(db, make_user):
    creator = make_user("Real Owner")
    channel = channels.create_channel(db, actor=creator, name="Owned")
    outsider = make_user("Not The Owner")
    target = make_user("Would Be Invited")
    channels.join_channel(db, actor=outsider, channel=channel)
    db.commit()

    with pytest.raises(PermissionDeniedError):
        channels.invite_member(db, actor=outsider, channel=channel, invitee=target)


def test_installation_admin_can_invite_to_any_channel(db, make_user):
    creator = make_user("Member Owner")
    channel = channels.create_channel(db, actor=creator, name="Community")
    admin = make_user("Site Admin", admin=True)
    invitee = make_user("Brought In")
    db.commit()

    channels.invite_member(db, actor=admin, channel=channel, invitee=invitee)
    db.commit()
    assert _is_member(db, channel, invitee)


# ---------------------------------------------------------------------------
# Removal
# ---------------------------------------------------------------------------


def test_channel_admin_can_remove_a_member(db, make_user):
    creator = make_user("Remover Owner")
    channel = channels.create_channel(db, actor=creator, name="Cleanup")
    member = make_user("To Be Removed")
    channels.join_channel(db, actor=member, channel=channel)
    db.commit()

    channels.remove_member(db, actor=creator, channel=channel, member=member)
    db.commit()
    assert not _is_member(db, channel, member)


def test_the_channel_creator_cannot_be_removed(db, make_user):
    creator = make_user("Undeletable Owner")
    channel = channels.create_channel(db, actor=creator, name="Owned Forever")
    admin = make_user("Site Admin Two", admin=True)
    db.commit()

    with pytest.raises(PermissionDeniedError) as exc:
        channels.remove_member(db, actor=admin, channel=channel, member=creator)
    assert exc.value.code == "CANNOT_REMOVE_CREATOR"
    assert _is_member(db, channel, creator)


def test_a_member_cannot_remove_others(db, make_user):
    creator = make_user("Owner Three")
    channel = channels.create_channel(db, actor=creator, name="Guarded")
    a = make_user("Member A")
    b = make_user("Member B")
    channels.join_channel(db, actor=a, channel=channel)
    channels.join_channel(db, actor=b, channel=channel)
    db.commit()

    with pytest.raises(PermissionDeniedError):
        channels.remove_member(db, actor=a, channel=channel, member=b)


# ---------------------------------------------------------------------------
# Invite links
# ---------------------------------------------------------------------------


def test_generate_and_join_via_invite_link(db, make_user):
    creator = make_user("Link Owner")
    channel = channels.create_channel(db, actor=creator, name="Link Channel")
    db.commit()

    code = channels.generate_invite_code(db, actor=creator, channel=channel)
    db.commit()
    assert code and len(code) >= 8

    joiner = make_user("Link Joiner")
    joined = channels.join_by_invite(db, actor=joiner, invite_code=code)
    db.commit()
    assert joined.id == channel.id
    assert _is_member(db, channel, joiner)


def test_regenerating_the_link_invalidates_the_old_one(db, make_user):
    creator = make_user("Rotator Owner")
    channel = channels.create_channel(db, actor=creator, name="Rotating")
    db.commit()
    old = channels.generate_invite_code(db, actor=creator, channel=channel)
    db.commit()
    new = channels.generate_invite_code(db, actor=creator, channel=channel)
    db.commit()
    assert old != new

    with pytest.raises(NotFoundError):
        channels.join_by_invite(db, actor=make_user("Late Joiner"), invite_code=old)


def test_revoked_link_cannot_be_used(db, make_user):
    creator = make_user("Revoker Owner")
    channel = channels.create_channel(db, actor=creator, name="Revokable")
    db.commit()
    code = channels.generate_invite_code(db, actor=creator, channel=channel)
    db.commit()

    channels.revoke_invite_code(db, actor=creator, channel=channel)
    db.commit()
    with pytest.raises(NotFoundError) as exc:
        channels.join_by_invite(db, actor=make_user("Blocked Joiner"), invite_code=code)
    assert exc.value.code == "INVITE_INVALID"


def test_only_channel_admin_can_manage_the_invite_link(db, make_user):
    creator = make_user("Sole Owner")
    channel = channels.create_channel(db, actor=creator, name="Locked Down")
    intruder = make_user("Intruder")
    channels.join_channel(db, actor=intruder, channel=channel)
    db.commit()

    with pytest.raises(PermissionDeniedError):
        channels.generate_invite_code(db, actor=intruder, channel=channel)


def test_invite_code_is_not_exposed_in_channel_payloads(client, make_user, sign_in, db):
    creator = make_user("Payload Owner")
    channel = channels.create_channel(db, actor=creator, name="Secret Code")
    channels.generate_invite_code(db, actor=creator, channel=channel)
    db.commit()

    sign_in(creator)
    body = client.get(f"/api/channels/{channel.id}").text
    assert channel.invite_code is not None
    assert channel.invite_code not in body


# ---------------------------------------------------------------------------
# Through the HTTP layer
# ---------------------------------------------------------------------------


def test_invite_flow_through_the_api(client, make_user, sign_in, db):
    creator = make_user("API Owner")
    channel = channels.create_channel(db, actor=creator, name="API Channel")
    invitee = make_user("API Invitee")
    db.commit()

    sign_in(creator)
    # Create an invite link.
    code_response = client.post(f"/api/channels/{channel.id}/invite-code")
    assert code_response.status_code == 200
    payload = code_response.json()
    assert payload["invite_url"].endswith(payload["invite_code"])

    # Directly invite a member.
    invite = client.post(
        f"/api/channels/{channel.id}/members", json={"user_id": str(invitee.id)}
    )
    assert invite.status_code == 200
    assert _is_member(db, channel, invitee)

    # Remove them again.
    removed = client.request(
        "DELETE", f"/api/channels/{channel.id}/members/{invitee.id}"
    )
    assert removed.status_code == 200
    db.expire_all()
    assert not _is_member(db, db.get(Channel, channel.id), invitee)


def test_join_by_code_through_the_api(client, make_user, sign_in, db):
    creator = make_user("Code Owner")
    channel = channels.create_channel(db, actor=creator, name="Joinable")
    code = channels.generate_invite_code(db, actor=creator, channel=channel)
    db.commit()

    joiner = make_user("Code Joiner")
    sign_in(joiner)
    response = client.post("/api/channels/join-by-code", json={"invite_code": code})
    assert response.status_code == 200
    assert response.json()["slug"] == channel.slug
    assert _is_member(db, channel, joiner)


def test_join_link_page_requires_sign_in_then_joins(client, make_user, sign_in, db):
    creator = make_user("Web Link Owner")
    channel = channels.create_channel(db, actor=creator, name="Web Link Channel")
    code = channels.generate_invite_code(db, actor=creator, channel=channel)
    db.commit()

    # Signed out: redirected to sign-in, preserving the destination.
    from app.core.config import settings

    anon = client
    anon.cookies.clear()
    redirected = anon.get(f"/channels/join/{code}", follow_redirects=False)
    assert redirected.status_code in (302, 303)
    assert "/signin" in redirected.headers["location"]

    joiner = make_user("Web Link Joiner")
    sign_in(joiner)
    joined = client.get(f"/channels/join/{code}", follow_redirects=False)
    assert joined.status_code == 303
    assert joined.headers["location"] == f"/channels/{channel.slug}"
    assert _is_member(db, channel, joiner)
    del settings


def test_manage_panel_shows_invite_controls_to_the_channel_admin(
    client, make_user, sign_in, db
):
    creator = make_user("Panel Owner")
    channel = channels.create_channel(db, actor=creator, name="Panel Channel")
    make_user("Someone To Invite")
    db.commit()

    sign_in(creator)
    page = client.get(f"/channels/{channel.slug}").text
    assert "Invite a member" in page
    assert "Invite link" in page
    assert "Create an invite link" in page


def test_manage_panel_hidden_from_non_admins(client, make_user, sign_in, db):
    creator = make_user("Hidden Owner")
    channel = channels.create_channel(db, actor=creator, name="Hidden Panel")
    member = make_user("Plain Viewer")
    channels.join_channel(db, actor=member, channel=channel)
    db.commit()

    sign_in(member)
    page = client.get(f"/channels/{channel.slug}").text
    assert "Invite a member" not in page
    assert "Create an invite link" not in page
