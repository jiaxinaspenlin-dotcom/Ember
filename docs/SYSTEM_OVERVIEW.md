# System overview

Ember is a cohort communications platform. This document explains what the
system is, what it holds, and how a request flows through it.

## The product in one sentence

A cohort talks in channels and direct messages; Ember turns the important parts
of that conversation into help requests, decisions, tasks and pinned resources
that outlive the scroll.

## The pieces

| Piece | Technology | Responsibility |
| --- | --- | --- |
| Web UI | Jinja2 templates, HTMX, a little Alpine.js, Tailwind CSS | Render what Python computed; collect form input |
| Application | FastAPI (Python 3.12) | Authentication, authorization, all business rules |
| Data | PostgreSQL 14+ | Every user-created record, plus the audit trail |

There is exactly **one deployable service**. The HTML UI and the JSON API are
two thin presentation layers over the same service functions.

## Domain model, in words

- A **User** has one **Profile** (skills, current project, working status), and
  authenticates with a **PasswordCredential**, one or more **OAuthAccounts**, or
  both. A signed-in browser holds a **UserSession**. Password reset and email
  verification use single-use **EmailToken**s (hash-at-rest).
- An **admin** creates **Channels**. Members join them (**ChannelMember**) and
  post **Messages**.
- Two members share exactly one **DirectConversation**
  (**DirectConversationMember**), which also holds **Messages**.
- A `Message` belongs to exactly one destination — a channel *or* a
  conversation — enforced by a database CHECK constraint. A message may have a
  `parent_message_id`, which makes it a thread reply.
- Messages accumulate **Reactions** (unique per user/type/message) and
  **Mentions** (parsed server-side).
- **ReadReceipts** record how far each user has read in each channel and
  conversation. Unread counts are derived from them in SQL.
- A public channel message can become a **HelpRequest**, a **Decision**, a
  **Task**, or a pinned message. Each keeps a link back to the original.
- **Notifications** are private per-recipient rows. **Announcements** are
  admin-authored and cohort-wide. **AuditEvents** record who did what.

## Request lifecycle

A page request, for example `GET /channels/launch-week`:

1. **Session** — the `ember_session` cookie is hashed and looked up. No valid
   session ⇒ redirect to `/signin?next=…`.
2. **Resource** — `channels.get_channel_by_slug()`; a miss raises `NotFoundError`
   which the error handler renders as a 404 page.
3. **Permissions** — `permissions.channel_capabilities()` returns
   `{can_post, can_join, can_leave, can_manage, can_pin, is_member}`.
4. **Data** — one page of messages (cursor-paginated), their reactions eagerly
   loaded, plus pins and the member count. Thread replies are *not* loaded.
5. **Read receipt** — if the viewer is a member and the channel has messages,
   their read position advances; the transaction commits.
6. **Render** — the template receives finished values: capability booleans,
   formatted timestamps, grouped reaction counts.
7. **Poll** — the page contains one element that re-fetches
   `/hx/channels/launch-week/stream?after_seq=N` every 4 seconds and swaps
   itself for any new rows plus an updated cursor.

A mutation, for example sending a message:

1. HTMX posts the form to `/hx/channels/{slug}/messages`.
2. The route calls `messages.create_message()`, which validates the body,
   enforces membership and the archived-channel rule, inserts the message,
   parses mentions, creates notification rows, advances the author's read
   receipt and writes an audit event.
3. The route commits. If anything raised, the transaction rolls back and a
   structured error is rendered as an inline banner — never a silent success.
4. The response is the rendered message row plus an out-of-band cursor update.

## What is deliberately *not* in the browser

- permission checks
- unread counts
- search
- help-request, decision and task state transitions
- mention resolution
- any notion of "who is an admin"

The client cannot fabricate any of these, because it never computes them.

## Scale target

Designed for cohorts of roughly 30 active members. At a 4-second poll interval
that is ~7.5 requests/second of polling, each answered by a single indexed query
returning at most 50 rows — and usually zero. See
[POLLING.md](POLLING.md) for the arithmetic.
