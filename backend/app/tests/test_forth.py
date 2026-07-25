"""Forth integration — validation, cohort link, action-item links, message cards.

Forth is a *link-only* integration: no API calls, no shared auth, no fetched
task data. These tests lock down the server-side validation and permissions and
confirm the UI renders (and escapes) correctly.
"""

from __future__ import annotations

import pytest

from app.core.errors import PermissionDeniedError, ValidationError
from app.services import cohorts, decisions, forth, help_requests, messages, tasks

VALID = "https://forth-bice.vercel.app/board/42"

VALID_URLS = [
    "https://forth-bice.vercel.app",
    "https://forth-bice.vercel.app/",
    "https://forth-bice.vercel.app/board/42",
    "https://forth-bice.vercel.app/t/9?tab=open#top",
]

# Every one of these must be rejected -- lookalikes, unsafe schemes, junk.
INVALID_URLS = [
    "http://forth-bice.vercel.app/",              # not https
    "ftp://forth-bice.vercel.app/",               # not https
    "https://forth-bice.vercel.app.evil.com/",    # suffix lookalike
    "https://evilforth-bice.vercel.app/",         # prefix lookalike
    "https://forth-bice.vercel.app@evil.com/",    # userinfo trick
    "https://evil.com/?u=forth-bice.vercel.app",  # host is evil.com
    "https://forth-bice-vercel.app/",             # different host
    "javascript:alert(1)",                        # unsafe scheme
    "data:text/html,<script>alert(1)</script>",   # unsafe scheme
    "not a url at all",
    "https://",
]


# ---------------------------------------------------------------------------
# URL validation (parsed components, never string containment)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("url", VALID_URLS)
def test_valid_forth_urls_accepted(url):
    assert forth.is_forth_url(url) is True
    assert forth.normalize_forth_url(url) == url


@pytest.mark.parametrize("url", INVALID_URLS)
def test_invalid_forth_urls_rejected(url):
    assert forth.is_forth_url(url) is False
    with pytest.raises(ValidationError) as exc:
        forth.normalize_forth_url(url)
    assert exc.value.code == "FORTH_URL_INVALID"


def test_empty_url_normalizes_to_none():
    assert forth.normalize_forth_url("") is None
    assert forth.normalize_forth_url("   ") is None
    assert forth.normalize_forth_url(None) is None


def test_extract_forth_links_finds_only_forth_hosts():
    body = (
        "docs at https://forth-bice.vercel.app/t/1, mirror https://evil.com/x, "
        "again https://forth-bice.vercel.app/t/1 and https://forth-bice.vercel.app/t/2."
    )
    assert forth.extract_forth_links(body) == [
        "https://forth-bice.vercel.app/t/1",
        "https://forth-bice.vercel.app/t/2",
    ]
    assert forth.extract_forth_links("no links here") == []


# ---------------------------------------------------------------------------
# Cohort workspace link (admin-only, cohort-scoped)
# ---------------------------------------------------------------------------


def test_admin_can_set_and_remove_forth_workspace_url(db, make_user):
    admin = make_user("Forth Admin", admin=True)
    cohorts.set_forth_workspace_url(db, actor=admin.membership, cohort=admin.cohort, url=VALID)
    db.commit()
    assert admin.cohort.forth_workspace_url == VALID

    # Removing (empty value) clears it.
    cohorts.set_forth_workspace_url(db, actor=admin.membership, cohort=admin.cohort, url="")
    db.commit()
    assert admin.cohort.forth_workspace_url is None


def test_non_admin_cannot_set_forth_workspace_url(db, make_user):
    member = make_user("Plain Member")
    with pytest.raises(PermissionDeniedError):
        cohorts.set_forth_workspace_url(
            db, actor=member.membership, cohort=member.cohort, url=VALID
        )
    assert member.cohort.forth_workspace_url is None


def test_cross_cohort_forth_edit_is_rejected(db, make_user, make_cohort):
    admin = make_user("Cohort A Admin", admin=True)
    other_cohort = make_cohort("Cohort B")
    with pytest.raises(PermissionDeniedError):
        cohorts.set_forth_workspace_url(
            db, actor=admin.membership, cohort=other_cohort, url=VALID
        )
    assert other_cohort.forth_workspace_url is None


def test_setting_an_invalid_workspace_url_is_rejected(db, make_user):
    admin = make_user("Strict Admin", admin=True)
    with pytest.raises(ValidationError):
        cohorts.set_forth_workspace_url(
            db, actor=admin.membership, cohort=admin.cohort, url="http://forth-bice.vercel.app/"
        )


# ---------------------------------------------------------------------------
# Forth links on tasks / decisions / help requests
# ---------------------------------------------------------------------------


def test_task_forth_url_create_update_and_remove(db, make_user):
    admin = make_user("Task Owner", admin=True)
    task = tasks.create_task(db, creator=admin.membership, title="Wire deploy", forth_url=VALID)
    db.commit()
    assert task.forth_url == VALID

    tasks.update_task(db, actor=admin.membership, task=task, clear_forth_url=True)
    db.commit()
    assert task.forth_url is None

    with pytest.raises(ValidationError):
        tasks.create_task(db, creator=admin.membership, title="Bad link", forth_url="https://evil.com")


def test_decision_and_help_accept_and_reject_forth_urls(db, make_user):
    author = make_user("Author", admin=True)
    decision = decisions.create_decision(
        db, author=author.membership, title="Adopt Forth", decision_text="Yes.", forth_url=VALID
    )
    help_request = help_requests.create_help_request(
        db, requester=author.membership, title="Need review", description="Please", forth_url=VALID
    )
    db.commit()
    assert decision.forth_url == VALID
    assert help_request.forth_url == VALID

    # Removal on update.
    decisions.update_decision(
        db, actor=author.membership, decision=decision, clear_forth_url=True
    )
    help_requests.update_help_request(
        db, actor=author.membership, help_request=help_request, clear_forth_url=True
    )
    db.commit()
    assert decision.forth_url is None
    assert help_request.forth_url is None

    with pytest.raises(ValidationError):
        decisions.create_decision(
            db, author=author.membership, title="Nope", decision_text="x",
            forth_url="https://forth-bice.vercel.app.evil.com/",
        )


# ---------------------------------------------------------------------------
# Messages: link detection on the view model
# ---------------------------------------------------------------------------


def test_message_view_exposes_forth_links(db, make_user, make_channel):
    admin = make_user("Poster", admin=True)
    channel = make_channel(admin, "Links")
    message = messages.create_message(
        db,
        actor=admin.membership,
        channel=channel,
        body="board https://forth-bice.vercel.app/t/1 and site https://evil.com",
    )
    db.commit()
    view = messages.build_view(db, message, viewer=admin.membership)
    assert view.forth_links == ["https://forth-bice.vercel.app/t/1"]


# ---------------------------------------------------------------------------
# Web rendering + escaping
# ---------------------------------------------------------------------------


def test_message_forth_card_renders_on_the_channel_page(
    client, db, make_user, sign_in, make_channel
):
    admin = make_user("Card Admin", admin=True)
    channel = make_channel(admin, "General")
    messages.create_message(
        db, actor=admin.membership, channel=channel,
        body="ship it https://forth-bice.vercel.app/board/9",
    )
    db.commit()
    sign_in(admin)
    page = client.get(f"/channels/{channel.slug}")
    assert page.status_code == 200
    assert "Open in Forth" in page.text
    assert "forth-bice.vercel.app/board/9" in page.text
    assert 'href="https://forth-bice.vercel.app/board/9"' in page.text


def test_unsafe_message_content_stays_escaped_with_a_forth_card(
    client, db, make_user, sign_in, make_channel
):
    admin = make_user("XSS Admin", admin=True)
    channel = make_channel(admin, "Safety")
    messages.create_message(
        db, actor=admin.membership, channel=channel,
        body="<script>alert('x')</script> https://forth-bice.vercel.app/x",
    )
    db.commit()
    sign_in(admin)
    page = client.get(f"/channels/{channel.slug}")
    assert "<script>alert('x')</script>" not in page.text  # never rendered raw
    assert "&lt;script&gt;" in page.text  # escaped instead
    assert "Open in Forth" in page.text  # card still shown alongside


def test_admin_sets_forth_workspace_link_via_web(client, db, make_user, sign_in):
    admin = make_user("Web Admin", admin=True)
    sign_in(admin)
    resp = client.post("/cohort/forth-link", data={"forth_url": VALID}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/admin#forth"
    db.refresh(admin.cohort)
    assert admin.cohort.forth_workspace_url == VALID
    # The "Open Forth" button now appears in the interface (sidebar + admin).
    assert "Open Forth" in client.get("/").text


def test_non_admin_web_set_forth_link_is_rejected(client, db, make_user, sign_in):
    member = make_user("Web Member")
    sign_in(member)
    resp = client.post("/cohort/forth-link", data={"forth_url": VALID}, follow_redirects=False)
    assert resp.status_code == 303  # handled, not a crash
    db.refresh(member.cohort)
    assert member.cohort.forth_workspace_url is None  # unchanged


def test_web_invalid_forth_link_shows_error_and_saves_nothing(client, db, make_user, sign_in):
    admin = make_user("Careful Admin", admin=True)
    sign_in(admin)
    resp = client.post(
        "/cohort/forth-link",
        data={"forth_url": "http://forth-bice.vercel.app/"},
        follow_redirects=False,
    )
    assert "forth_error=1" in resp.headers["location"]
    db.refresh(admin.cohort)
    assert admin.cohort.forth_workspace_url is None
