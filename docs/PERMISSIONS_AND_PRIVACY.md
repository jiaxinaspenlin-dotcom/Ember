# Permissions and privacy

Every authorization decision is made in `app/auth/permissions.py` and enforced by
the service layer. The browser is never trusted, and hiding a control in a
template is never the mechanism — it is only the cosmetic consequence of a
decision Python already made.

**Multi-tenancy.** Ember is cohort-scoped. On top of the checks below, every read
is fenced to the caller's **active cohort**: a resource in another cohort is a
**404**, never a 403. `role` is per-cohort (`admin` of *this* cohort, not the
install), and there is no global installation admin. See [COHORTS.md](COHORTS.md).

## The checks every route performs

1. **Authentication** — a valid, unexpired, unrevoked session
2. **Current user** — resolved from the session, not from any request field
3. **Active cohort** — resolved from the session; a request with none gets
   `409 NO_ACTIVE_COHORT` (API) or a redirect to the cohort picker (web)
4. **Role** — `member` or `admin` **within the active cohort**, read from the
   `cohort_memberships` row
4. **Channel membership** — for posting
5. **Conversation membership** — for every DM read and write
6. **Resource ownership** — author, requester, creator, assignee, recipient
7. **Action permission** — including state-machine legality

## Permission matrix

### Channels

| Action | Member | Channel member | Creator | Admin |
| --- | --- | --- | --- | --- |
| View a public channel | ✅ | ✅ | ✅ | ✅ |
| Create a channel | ✅ | ✅ | — | ✅ |
| Join / leave | ✅ (not archived) | ✅ | ✅ | ✅ |
| Post a message | ❌ | ✅ (not archived) | ✅ | ✅ (if a member) |
| Reply in a thread | ❌ | ✅ (not archived) | ✅ | ✅ |
| Rename / archive / restore | ❌ | ❌ | ✅ (own) | ✅ (any) |
| Invite / remove members | ❌ | ❌ | ✅ (own) | ✅ (any) |
| Manage the invite link | ❌ | ❌ | ✅ (own) | ✅ (any) |
| Pin a message | ❌ | ❌ | ✅ (own) | ✅ (any) |

Any member may **create** a channel; the **creator is that channel's admin**
(rename, archive, restore, pin, invite and remove members, manage the invite
link), and installation **admins** manage any channel. Being a channel admin
does **not** grant the installation-wide `admin` role — it is scoped to channels
you created. The creator can never be removed from their own channel.

**Invite links** are opaque random codes stored on the channel (`invite_code`),
never exposed in ordinary channel payloads. Anyone in the cohort with the link
can join; regenerating or turning it off invalidates the old link immediately.
Channels remain public — invites are a convenience for pulling people in, not a
privacy boundary.

Archived channels are readable and searchable by everyone, and reject all
writes including thread replies.

### Messages

| Action | Author | Admin | Anyone else |
| --- | --- | --- | --- |
| Edit | ✅ (24 h) | ❌ | ❌ |
| Delete (soft) | ✅ | ✅ | ❌ |
| Pin | — | ✅ (any channel) | channel creator only |
| React | ✅ | ✅ | ✅ (if they can see it) |
| Convert to help/decision/task | ✅ | ✅ | ✅ (channel messages only) |

Admins can *remove* an inappropriate message but cannot *rewrite* it — deletion
is auditable, silent authorship changes would not be.

### Direct messages

| Action | Participant | Admin | Anyone else |
| --- | --- | --- | --- |
| List conversations | own only | own only | own only |
| Read messages | ✅ | ❌ | ❌ |
| Send | ✅ | ❌ | ❌ |
| Appear in search | ✅ | ❌ | ❌ |
| Convert to a cohort item | ❌ | ❌ | ❌ |

**Administrators have no special access to direct messages.** A non-participant
receives `404 CONVERSATION_NOT_FOUND`, not 403 — the existence of a conversation
is itself private.

### Help requests

| Action | Requester | Assigned helper | Admin | Other member |
| --- | --- | --- | --- | --- |
| Create | ✅ | ✅ | ✅ | ✅ |
| Claim | ❌ (own) | — | ✅ | ✅ |
| Unclaim | ❌ | ✅ | ✅ | ❌ |
| Resolve | ✅ | ✅ | ✅ | ❌ |
| Cancel | ✅ | ❌ | ✅ | ❌ |
| Reopen | ✅ | ❌ | ✅ | ❌ |
| Edit | ✅ | ❌ | ✅ | ❌ |

### Decisions

| Action | Author | Admin | Other member |
| --- | --- | --- | --- |
| Create | ✅ | ✅ | ✅ |
| Read | ✅ | ✅ | ✅ |
| Edit (while active) | ✅ | ✅ | ❌ |
| Supersede | ✅ | ✅ | ❌ |
| Reverse | ✅ | ✅ | ❌ |
| Delete | ❌ | ❌ | ❌ |

Decisions are never deleted — history is the point of the log.

### Tasks

| Action | Creator | Assignee | Admin | Other member |
| --- | --- | --- | --- | --- |
| Create | ✅ | ✅ | ✅ | ✅ |
| Read | ✅ | ✅ | ✅ | ✅ |
| Edit title/description/priority/due | ✅ | ❌ | ✅ | ❌ |
| Assign / reassign | ✅ | ❌ | ✅ | ❌ |
| Change status | ✅ | ✅ | ✅ | ❌ |

### Notifications, announcements, admin

- Notifications are visible only to their `recipient_id`. Acting on someone
  else's returns 404.
- Only administrators create, edit or delete announcements; every member reads
  them.
- `/admin` and `/api/admin/*` return 403 for members.

## Separate query and schema paths

Different audiences get different shapes, by construction:

| Audience | Schema | Contains |
| --- | --- | --- |
| Yourself | `CurrentUserOut` | id, **your** email, verified flag, role, timestamps |
| Other members | `UserSummary` | id, display name, avatar, role — **no email field exists** |
| Channel lists | `ChannelListItemOut` | channel + *your* membership and *your* unread count |
| Dashboard | `DashboardResponse` | only your counts and your assignments |
| Notifications | `NotificationOut` | filtered by `recipient_id` in SQL |
| Admin | `AdminStats`, audit rows | admin-gated dependency |

## Search privacy

The permission filter is part of the SQL statement, not a post-filter:

```python
dm_visible = Message.direct_conversation_id.in_(
    select(DirectConversationMember.conversation_id)
    .where(DirectConversationMember.user_id == user_id)
)
stmt = select(Message).where(
    or_(Message.channel_id.is_not(None), dm_visible),
    Message.deleted_at.is_(None),
)
```

A user therefore cannot receive:

- direct messages from conversations they are not in
- soft-deleted content
- another user's notifications
- unbounded results (capped at 100, paginated)

## Never exposed

Password hashes · session tokens · GitHub tokens · `ADMIN_EMAILS` /
`ADMIN_GITHUB_USERNAMES` · other users' email addresses · admin-only controls ·
soft-deleted message bodies · channel invite codes (except to the channel's
admin) · another user's notifications, read receipts or unread counts.

## Logging

`app/core/logging.py::scrub()` redacts a fixed key list (passwords, tokens,
cookies, authorization headers, message bodies) from anything attached to a log
record or an audit row. Database failures are logged by exception *type* only —
no SQL, no parameters. A test asserts a distinctive message body never reaches
`audit_events.context`.

## Tests

`app/tests/test_permissions.py` covers every boundary above: anonymous access,
member vs admin, channel membership, DM privacy, search boundaries, unauthorized
task updates, unauthorized channel modification, and both state machines.
Per-channel admin, invitations, member removal and invite links are covered by
`test_channel_invites.py`. Additional privacy assertions live in
`test_search_and_dashboard.py` and `test_web_flows.py`.
