"""The empty-database experience, structured errors, and rendered pages.

An installation with zero users, channels and messages must be fully usable --
never a crash, never fabricated content.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select

from app.models.action import Decision, HelpRequest, Task
from app.models.channel import Channel
from app.models.engagement import Announcement, Notification
from app.models.message import Message
from app.models.user import User
from app.services import channels, messages

# ---------------------------------------------------------------------------
# Empty database
# ---------------------------------------------------------------------------


def test_the_database_starts_completely_empty(db):
    for model in (
        User,
        Channel,
        Message,
        Notification,
        Announcement,
        HelpRequest,
        Decision,
        Task,
    ):
        assert db.scalar(select(func.count()).select_from(model)) == 0, model.__name__


def test_startup_creates_no_records(client, db):
    """Booting the application must never insert anything."""

    client.get("/api/health")
    assert db.scalar(select(func.count()).select_from(User)) == 0
    assert db.scalar(select(func.count()).select_from(Channel)) == 0


def test_signup_creates_only_the_new_account(client, db):
    client.post(
        "/api/auth/signup",
        json={
            "email": "solo@embercohort.dev",
            "password": "correct-horse-9",
            "display_name": "Solo Person",
        },
    )
    assert db.scalar(select(func.count()).select_from(User)) == 1
    # No channels, no messages, no announcements were conjured up.
    assert db.scalar(select(func.count()).select_from(Channel)) == 0
    assert db.scalar(select(func.count()).select_from(Message)) == 0
    assert db.scalar(select(func.count()).select_from(Announcement)) == 0


def test_all_list_endpoints_return_valid_empty_responses(client, make_user, sign_in):
    user = make_user("First Member")
    sign_in(user)

    for path in (
        "/api/channels",
        "/api/direct-messages",
        "/api/notifications",
        "/api/announcements",
        "/api/help-requests",
        "/api/decisions",
        "/api/tasks",
        "/api/members",
    ):
        response = client.get(path)
        assert response.status_code == 200, path
        body = response.json()
        assert body["items"] == [], path
        assert body["total"] == 0, path
        assert body["has_more"] is False, path


def test_dashboard_endpoint_works_with_no_content(client, make_user, sign_in):
    user = make_user("Empty Dashboard User")
    sign_in(user)
    response = client.get("/api/dashboard")
    assert response.status_code == 200
    body = response.json()
    assert body["unread_messages"] == 0
    assert body["active_channels"] == 0
    assert body["recent_announcements"] == []
    assert body["my_tasks"] == []


def test_search_with_no_content_returns_no_results(client, make_user, sign_in):
    user = make_user("Empty Search User")
    sign_in(user)
    response = client.get("/api/search", params={"q": "anything at all"})
    assert response.status_code == 200
    assert response.json()["results"] == []
    assert response.json()["total"] == 0


# ---------------------------------------------------------------------------
# Rendered pages
# ---------------------------------------------------------------------------


def test_empty_pages_render_their_empty_states(client, make_user, sign_in):
    user = make_user("Empty Pages User")
    sign_in(user)

    expectations = {
        "/channels": "No channels yet.",
        "/dm": "No conversations yet. Message a cohort member.",
        "/help": "No help requests are open.",
        "/decisions": "No decisions have been recorded.",
        "/tasks": "No tasks yet.",
        "/notifications": "You&#39;re all caught up.",
        "/members": "No other members have joined yet.",
        "/announcements": "No announcements yet.",
    }
    for path, expected in expectations.items():
        response = client.get(path)
        assert response.status_code == 200, path
        assert expected in response.text, f"{path} missing empty state: {expected}"


def test_empty_state_is_not_an_error_state(client, make_user, sign_in):
    user = make_user("Distinct States User")
    sign_in(user)
    body = client.get("/channels").text
    assert "empty-state" in body
    assert "error-state" not in body


def test_channel_page_shows_the_start_the_conversation_empty_state(
    client, db, make_user, sign_in, make_channel
):
    admin = make_user("Empty Channel Admin", admin=True)
    channel = make_channel(admin, "Quiet Channel")
    sign_in(admin)

    response = client.get(f"/channels/{channel.slug}")
    assert response.status_code == 200
    assert "No messages yet. Start the conversation." in response.text


def test_home_page_guides_the_first_admin_to_create_a_channel(
    client, make_user, sign_in, db
):
    admin = make_user("Guided Admin", admin=True)
    admin.profile_completed = True
    db.commit()
    sign_in(admin)

    response = client.get("/")
    assert response.status_code == 200
    assert "Create the first channel" in response.text


def test_signed_out_pages_render(client):
    for path in ("/signin", "/signup"):
        response = client.get(path)
        assert response.status_code == 200
        assert "Ember" in response.text


def test_private_pages_redirect_to_sign_in_when_signed_out(client):
    for path in ("/", "/channels", "/dm", "/help", "/tasks", "/decisions", "/members"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 303, path
        assert response.headers["location"].startswith("/signin"), path


def test_pages_are_inaccessible_after_logout(client, make_user, sign_in):
    user = make_user("Logout Page User")
    sign_in(user)
    assert client.get("/channels", follow_redirects=False).status_code == 200

    client.post("/signout", follow_redirects=False)
    after = client.get("/channels", follow_redirects=False)
    assert after.status_code == 303
    assert after.headers["location"].startswith("/signin")


# ---------------------------------------------------------------------------
# Structured errors
# ---------------------------------------------------------------------------


def test_errors_use_the_documented_envelope(client, make_user, sign_in):
    member = make_user("Envelope Member")
    sign_in(member)
    # Announcements remain admin-only, so this is a reliable 403 example.
    response = client.post("/api/announcements", json={"title": "Denied post", "body": "No."})

    payload = response.json()
    assert set(payload) == {"error"}
    assert payload["error"]["code"] == "PERMISSION_DENIED"
    assert isinstance(payload["error"]["message"], str)
    assert payload["error"]["retryable"] is False


def test_validation_errors_name_the_field(client, make_user, sign_in, make_channel):
    admin = make_user("Validation Admin", admin=True)
    channel = make_channel(admin)
    sign_in(admin)

    response = client.post(f"/api/messages/channel/{channel.id}", json={"body": ""})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_FAILED"


def test_not_found_is_distinct_from_permission_denied(client, make_user, sign_in):
    user = make_user("Not Found User")
    sign_in(user)
    response = client.get(f"/api/channels/{uuid.uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "CHANNEL_NOT_FOUND"


def test_unauthenticated_error_marks_the_session_as_expired(client):
    response = client.get("/api/auth/me")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "SESSION_EXPIRED"
    assert response.json()["error"]["retryable"] is False


def test_state_transition_errors_are_specific(client, db, make_user, sign_in):
    from app.services import help_requests

    requester = make_user("Transition Requester")
    request = help_requests.create_help_request(
        db, requester=requester, title="Already resolved item", description="Body."
    )
    help_requests.resolve_help_request(db, actor=requester, help_request=request)
    db.commit()

    helper = make_user("Transition Helper")
    sign_in(helper)
    response = client.post(f"/api/help-requests/{request.id}/claim")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "HELP_REQUEST_INVALID_TRANSITION"


def test_htmx_errors_render_an_inline_banner(client, make_user, sign_in, db, make_channel):
    admin = make_user("HTMX Admin", admin=True)
    channel = make_channel(admin, "Htmx Channel")
    outsider = make_user("HTMX Outsider")
    sign_in(outsider)

    response = client.post(
        f"/hx/channels/{channel.slug}/messages",
        data={"body": "Let me in"},
        headers={"hx-request": "true"},
    )
    assert response.status_code == 403
    assert "error-state" in response.text
    assert "Join this channel before posting." in response.text


def test_failed_write_returns_an_error_rather_than_pretending_to_succeed(
    client, db, make_user, sign_in, make_channel
):
    admin = make_user("Archive Write Admin", admin=True)
    channel = make_channel(admin, "Closing Channel")
    channels.archive_channel(db, actor=admin, channel=channel)
    db.commit()

    sign_in(admin)
    response = client.post(f"/api/messages/channel/{channel.id}", json={"body": "Too late"})
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "CHANNEL_ARCHIVED"
    # Nothing was written.
    assert messages.list_messages(db, channel=channel) == []


def test_health_endpoints_report_database_connectivity(client):
    assert client.get("/api/health").json()["status"] == "ok"
    assert client.get("/api/health/ready").json()["database"] == "connected"
