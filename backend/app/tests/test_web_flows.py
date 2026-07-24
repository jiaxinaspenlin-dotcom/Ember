"""End-to-end journeys through the server-rendered interface.

These exercise the same service layer the JSON API uses, through real form
posts and HTMX fragment requests.
"""

from __future__ import annotations

import re

from sqlalchemy import select

from app.core.enums import HelpRequestStatus, TaskStatus
from app.models.action import Decision, HelpRequest, Task
from app.models.channel import Channel
from app.models.cohort import CohortMembership
from app.models.message import Message
from app.models.user import User
from app.services import channels, messages

HX = {"hx-request": "true"}


def test_full_signup_and_profile_setup_journey(client, db):
    signup = client.post(
        "/signup",
        data={
            "display_name": "Jamie Rivers",
            "email": "jamie@embercohort.dev",
            "password": "correct-horse-9",
        },
        follow_redirects=False,
    )
    assert signup.status_code == 303
    assert signup.headers["location"] == "/"

    # With no cohort yet, the app routes them to the picker.
    assert client.get("/", follow_redirects=False).headers["location"] == "/cohorts"
    picker = client.get("/cohorts")
    assert picker.status_code == 200
    assert "Your cohorts" in picker.text

    # Create a cohort -> becomes its admin -> on to profile setup.
    created = client.post(
        "/cohorts",
        data={"name": "Summer Builders", "description": "A demo cohort"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/profile/complete"

    setup_page = client.get("/profile/complete")
    assert setup_page.status_code == 200
    assert "Set up your profile" in setup_page.text

    saved = client.post(
        "/profile/complete",
        data={
            "display_name": "Jamie Rivers",
            "bio": "Building a scheduling tool.",
            "skills": "Python, Postgres",
            "current_project": "Shiftly",
            "project_area": "Operations",
            "working_status": "available_to_help",
            "available_to_help": "true",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert saved.headers["location"] == "/"

    user = db.scalar(select(User).where(User.email == "jamie@embercohort.dev"))
    membership = db.scalar(select(CohortMembership).where(CohortMembership.user_id == user.id))
    db.refresh(membership)
    assert membership.profile_completed is True
    assert membership.current_project == "Shiftly"
    assert set(membership.skill_names) == {"Python", "Postgres"}

    home = client.get("/")
    assert home.status_code == 200
    assert "Welcome back, Jamie" in home.text


def test_signup_with_a_taken_email_shows_an_error_not_a_crash(client, make_user):
    make_user("Taken Person", email="taken@embercohort.dev")
    response = client.post(
        "/signup",
        data={
            "display_name": "Copycat Person",
            "email": "taken@embercohort.dev",
            "password": "correct-horse-9",
        },
    )
    assert response.status_code == 409
    assert "already exists" in response.text
    assert "error-state" in response.text


def test_admin_creates_a_channel_and_members_join_and_post(client, db, make_user, sign_in):
    admin = make_user("Web Admin", admin=True)
    sign_in(admin)

    created = client.post(
        "/channels",
        data={"name": "Launch Week", "topic": "Everything about launch"},
        follow_redirects=False,
    )
    assert created.status_code == 303
    assert created.headers["location"] == "/channels/launch-week"

    channel = db.scalar(select(Channel).where(Channel.slug == "launch-week"))
    assert channel is not None

    page = client.get("/channels/launch-week")
    assert "Launch Week" in page.text
    assert "Everything about launch" in page.text

    sent = client.post(
        "/hx/channels/launch-week/messages",
        data={"body": "Kicking off the launch checklist"},
        headers=HX,
    )
    assert sent.status_code == 200
    assert "Kicking off the launch checklist" in sent.text

    stored = db.scalars(select(Message).where(Message.channel_id == channel.id)).all()
    assert len(stored) == 1


def test_non_member_sees_a_join_prompt_instead_of_the_composer(
    client, db, make_user, sign_in, make_channel
):
    admin = make_user("Prompt Admin", admin=True)
    channel = make_channel(admin, "Members Only")
    outsider = make_user("Prompt Outsider")
    sign_in(outsider)

    page = client.get(f"/channels/{channel.slug}")
    assert "Join this channel to post." in page.text
    assert 'id="composer"' not in page.text

    client.post(f"/channels/{channel.slug}/join", follow_redirects=False)
    after = client.get(f"/channels/{channel.slug}")
    assert 'id="composer"' in after.text


def test_archived_channel_page_is_read_only(client, db, make_user, sign_in, make_channel):
    admin = make_user("Readonly Admin", admin=True)
    channel = make_channel(admin, "Wrapping Up")
    sign_in(admin)
    client.post(f"/channels/{channel.slug}/archive", follow_redirects=False)

    page = client.get(f"/channels/{channel.slug}")
    assert "archived" in page.text.lower()
    assert "You can read and search it, but not post." in page.text
    assert 'id="composer"' not in page.text


def test_polling_fragment_returns_only_new_messages(client, db, make_user, sign_in, make_channel):
    admin = make_user("Poll Admin", admin=True)
    channel = make_channel(admin, "Polling Channel")
    sign_in(admin)

    client.post(f"/hx/channels/{channel.slug}/messages", data={"body": "First line"}, headers=HX)
    latest = messages.latest_seq(db, channel=channel)

    quiet = client.get(
        f"/hx/channels/{channel.slug}/stream", params={"after_seq": latest}, headers=HX
    )
    assert quiet.status_code == 200
    assert "First line" not in quiet.text
    assert 'id="message-poller"' in quiet.text

    client.post(f"/hx/channels/{channel.slug}/messages", data={"body": "Second line"}, headers=HX)
    busy = client.get(
        f"/hx/channels/{channel.slug}/stream", params={"after_seq": latest}, headers=HX
    )
    assert "Second line" in busy.text
    assert "First line" not in busy.text


def test_reaction_toggle_fragment_updates_the_count(client, db, make_user, sign_in, make_channel):
    admin = make_user("React Admin", admin=True)
    channel = make_channel(admin, "React Channel")
    message = messages.create_message(db, actor=admin, channel=channel, body="Ship it")
    db.commit()
    sign_in(admin)

    on = client.post(
        f"/hx/messages/{message.id}/react", data={"reaction_type": "celebration"}, headers=HX
    )
    assert on.status_code == 200
    assert 'aria-pressed="true"' in on.text

    off = client.post(
        f"/hx/messages/{message.id}/react", data={"reaction_type": "celebration"}, headers=HX
    )
    assert "aria-pressed" not in off.text


def test_thread_page_and_reply(client, db, make_user, sign_in, make_channel):
    admin = make_user("Thread Admin", admin=True)
    channel = make_channel(admin, "Thread Channel")
    parent = messages.create_message(
        db, actor=admin, channel=channel, body="What should we name it?"
    )
    db.commit()
    sign_in(admin)

    page = client.get(f"/threads/{parent.id}")
    assert page.status_code == 200
    assert "What should we name it?" in page.text
    assert "No replies yet." in page.text

    reply = client.post(
        f"/hx/threads/{parent.id}/replies", data={"body": "How about Ember?"}, headers=HX
    )
    assert reply.status_code == 200
    assert "How about Ember?" in reply.text

    db.refresh(parent)
    assert parent.reply_count == 1


def test_direct_message_journey(client, db, make_user, sign_in):
    alice = make_user("Alice Web")
    bob = make_user("Bob Web")
    sign_in(alice)

    started = client.post("/dm/start", data={"user_id": str(bob.id)}, follow_redirects=False)
    assert started.status_code == 303
    conversation_path = started.headers["location"]

    page = client.get(conversation_path)
    assert "Bob Web" in page.text
    assert "No messages yet. Start the conversation." in page.text

    conversation_id = conversation_path.rsplit("/", 1)[-1]
    sent = client.post(
        f"/hx/dm/{conversation_id}/messages", data={"body": "Want to pair later?"}, headers=HX
    )
    assert sent.status_code == 200
    assert "Want to pair later?" in sent.text

    listing = client.get("/dm")
    assert "Want to pair later?" in listing.text


def test_message_to_action_menu_creates_each_kind_of_item(
    client, db, make_user, sign_in, make_channel
):
    admin = make_user("Action Web Admin", admin=True)
    channel = make_channel(admin, "Actions Channel")
    message = messages.create_message(
        db, actor=admin, channel=channel, body="We are blocked on the deploy pipeline"
    )
    db.commit()
    sign_in(admin)

    menu = client.get(f"/messages/{message.id}/actions")
    assert menu.status_code == 200
    for label in ("Help request", "Decision", "Task", "Feedback request", "Pinned resource"):
        assert label in menu.text

    help_response = client.post(
        "/help",
        data={
            "title": "Unblock the deploy pipeline",
            "description": "Fails at the migration step.",
            "category": "deployment",
            "urgency": "high",
            "source_message_id": str(message.id),
        },
        follow_redirects=False,
    )
    assert help_response.status_code == 303
    request = db.scalars(select(HelpRequest)).one()
    assert request.original_message_id == message.id
    assert request.source_channel_id == channel.id

    decision_response = client.post(
        "/decisions",
        data={
            "title": "Pin the deploy pipeline version",
            "decision_text": "Stay on v2 until the migration is fixed.",
            "context": "v3 breaks migrations.",
            "related_project": "Platform",
            "source_message_id": str(message.id),
        },
        follow_redirects=False,
    )
    assert decision_response.status_code == 303
    decision = db.scalars(select(Decision)).one()
    assert decision.original_message_id == message.id

    task_response = client.post(
        "/tasks",
        data={
            "title": "Fix the migration step",
            "description": "Root cause the failure.",
            "assignee_id": str(admin.id),
            "priority": "urgent",
            "source_message_id": str(message.id),
        },
        follow_redirects=False,
    )
    assert task_response.status_code == 303
    task = db.scalars(select(Task)).one()
    assert task.source_message_id == message.id

    pin_response = client.post(f"/messages/{message.id}/pin-resource", follow_redirects=False)
    assert pin_response.status_code == 303
    db.refresh(message)
    assert message.is_pinned is True

    channel_page = client.get(f"/channels/{channel.slug}")
    assert "pinned" in channel_page.text.lower()


def test_help_queue_claim_and_resolve_through_the_ui(client, db, make_user, sign_in):
    requester = make_user("Queue Requester")
    helper = make_user("Queue Helper")

    sign_in(requester)
    client.post(
        "/help",
        data={
            "title": "Need help with indexing",
            "description": "Queries are slow.",
            "category": "coding",
            "urgency": "normal",
        },
        follow_redirects=False,
    )
    request = db.scalars(select(HelpRequest)).one()
    client.post("/signout", follow_redirects=False)

    sign_in(helper)
    queue = client.get("/help")
    assert "Need help with indexing" in queue.text
    assert "Claim" in queue.text

    client.post(f"/help/{request.id}/claim", follow_redirects=False)
    db.refresh(request)
    assert request.status is HelpRequestStatus.CLAIMED
    assert request.assigned_helper_id == helper.id

    client.post(
        f"/help/{request.id}/resolve",
        data={"resolution_note": "Added a composite index."},
        follow_redirects=False,
    )
    db.refresh(request)
    assert request.status.value == "resolved"

    detail = client.get(f"/help/{request.id}")
    assert "Added a composite index." in detail.text


def test_decision_supersede_through_the_ui(client, db, make_user, sign_in):
    author = make_user("Decision Author")
    sign_in(author)

    client.post(
        "/decisions",
        data={"title": "Use polling for updates", "decision_text": "Poll every four seconds."},
        follow_redirects=False,
    )
    client.post(
        "/decisions",
        data={"title": "Use server sent events", "decision_text": "Switch to SSE."},
        follow_redirects=False,
    )
    first, second = db.scalars(select(Decision).order_by(Decision.created_at)).all()

    client.post(
        f"/decisions/{first.id}/supersede",
        data={"superseded_by_id": str(second.id)},
        follow_redirects=False,
    )
    db.refresh(first)
    assert first.superseded_by_id == second.id

    detail = client.get(f"/decisions/{first.id}")
    assert "Superseded" in detail.text
    assert "Use server sent events" in detail.text


def test_task_status_change_through_the_htmx_fragment(client, db, make_user, sign_in):
    creator = make_user("Task Web Creator")
    sign_in(creator)
    client.post(
        "/tasks",
        data={"title": "Draft the retro notes", "assignee_id": str(creator.id)},
        follow_redirects=False,
    )
    task = db.scalars(select(Task)).one()

    response = client.post(
        f"/hx/tasks/{task.id}/status", data={"status": "in_progress"}, headers=HX
    )
    assert response.status_code == 200
    db.refresh(task)
    assert task.status is TaskStatus.IN_PROGRESS
    assert "In progress" in response.text


def test_notifications_page_and_mark_all_read(client, db, make_user, sign_in, make_channel):
    admin = make_user("Notify Web Admin", admin=True)
    channel = make_channel(admin, "Notify Channel")
    member = make_user("Notify Web Member")
    channels.join_channel(db, actor=member, channel=channel)
    db.commit()
    messages.create_message(db, actor=admin, channel=channel, body="@Notify-Web-Member take a look")
    db.commit()

    sign_in(member)
    page = client.get("/notifications")
    assert "mentioned you" in page.text

    badge = client.get("/hx/notifications/badge", headers=HX)
    assert "1" in badge.text

    client.post("/notifications/read-all", follow_redirects=False)
    cleared = client.get("/hx/notifications/badge", headers=HX)
    assert cleared.text.strip() == ""


def test_search_page_finds_content_and_respects_privacy(
    client, db, make_user, sign_in, make_channel
):
    admin = make_user("Search Web Admin", admin=True)
    channel = make_channel(admin, "Search Channel")
    messages.create_message(
        db, actor=admin, channel=channel, body="The quokka release is scheduled"
    )
    db.commit()

    member = make_user("Search Web Member")
    sign_in(member)
    results = client.get("/search", params={"q": "quokka"})
    assert results.status_code == 200
    assert "quokka" in results.text

    nothing = client.get("/search", params={"q": "zzzznotpresent"})
    assert "No results" in nothing.text


def test_members_directory_and_profile_page(client, db, make_user, sign_in):
    viewer = make_user("Directory Viewer")
    other = make_user("Directory Subject")
    from app.services import profiles

    profiles.update_profile(
        db,
        membership=other.membership,
        skills=["Rust"],
        current_project="Compiler work",
        available_to_help=True,
    )
    db.commit()

    sign_in(viewer)
    listing = client.get("/members")
    assert "Directory Subject" in listing.text
    assert "Compiler work" in listing.text
    assert other.email not in listing.text

    detail = client.get(f"/members/{other.id}")
    assert "Rust" in detail.text
    assert other.email not in detail.text


def test_admin_console_is_admin_only(client, make_user, sign_in):
    member = make_user("Console Member")
    sign_in(member)
    denied = client.get("/admin")
    assert denied.status_code == 403
    client.post("/signout", follow_redirects=False)

    admin = make_user("Console Admin", admin=True)
    sign_in(admin)
    allowed = client.get("/admin")
    assert allowed.status_code == 200
    assert "Members and roles" in allowed.text


def test_admin_can_promote_a_member_from_the_console(client, db, make_user, sign_in):
    admin = make_user("Promoter Admin", admin=True)
    member = make_user("Promotee Member")
    sign_in(admin)

    client.post(f"/admin/users/{member.id}/role", data={"role": "admin"}, follow_redirects=False)
    db.refresh(member.membership)
    assert member.membership.is_admin is True


def test_admin_cannot_demote_themselves(client, db, make_user, sign_in):
    admin = make_user("Self Demote Admin", admin=True)
    sign_in(admin)
    response = client.post(f"/admin/users/{admin.id}/role", data={"role": "member"})
    assert response.status_code == 409
    db.refresh(admin.membership)
    assert admin.membership.is_admin is True


def test_announcement_publishing_and_visibility(client, db, make_user, sign_in):
    admin = make_user("Announce Admin", admin=True)
    sign_in(admin)
    client.post(
        "/announcements",
        data={"title": "Demo day on Friday", "body": "Two minute updates.", "priority": "high"},
        follow_redirects=False,
    )

    page = client.get("/announcements")
    assert "Demo day on Friday" in page.text
    client.post("/signout", follow_redirects=False)

    member = make_user("Announce Member")
    sign_in(member)
    member_view = client.get("/announcements")
    assert "Demo day on Friday" in member_view.text
    # The publish form is not rendered for members.
    assert "Publish an announcement" not in member_view.text


def test_rendered_message_bodies_are_escaped(client, db, make_user, sign_in, make_channel):
    admin = make_user("XSS Admin", admin=True)
    channel = make_channel(admin, "Escaping Channel")
    messages.create_message(
        db,
        actor=admin,
        channel=channel,
        body="<script>alert('xss')</script> and <b>bold</b>",
    )
    db.commit()
    sign_in(admin)

    page = client.get(f"/channels/{channel.slug}")
    assert "<script>alert" not in page.text
    assert "&lt;script&gt;" in page.text


def test_pages_include_accessibility_landmarks(client, make_user, sign_in):
    user = make_user("A11y User")
    sign_in(user)
    page = client.get("/channels")

    assert 'aria-label="Main navigation"' in page.text
    assert 'id="main"' in page.text
    assert "Skip to main content" in page.text
    assert re.search(r'<html lang="en"', page.text)


def test_skip_profile_setup_lets_you_into_the_app(client, make_user, sign_in, db):
    """Skipping must not bounce straight back to the setup page."""

    user = make_user("Skipper Person", profile_completed=False)
    sign_in(user)

    # The home page sends an incomplete profile to setup...
    assert client.get("/", follow_redirects=False).headers["location"] == "/profile/complete"

    # ...but skipping marks it done and lands on home, not back on setup.
    skip = client.post("/profile/complete/skip", follow_redirects=False)
    assert skip.status_code == 303
    assert skip.headers["location"] == "/"

    db.refresh(user.membership)
    assert user.membership.profile_completed is True
    assert client.get("/", follow_redirects=False).status_code == 200


def test_topbar_left_padding_clears_the_hamburger(client, make_user, sign_in):
    """Regression: the search bar must not sit under the fixed menu button."""

    user = make_user("Topbar Person")
    sign_in(user)
    body = client.get("/channels").text
    # Left padding stays wide (pl-14) until `lg`, where the hamburger is hidden.
    assert "pl-14 sm:pr-6 lg:pl-6" in body
    # The old collapsing value must be gone.
    assert "px-4 py-3 pl-14 sm:px-6" not in body
