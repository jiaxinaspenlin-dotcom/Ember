# API reference

Base URL: the deployment origin (for example `http://localhost:8000`).
Interactive docs are served at `/api/docs` in non-production environments.

**Authentication:** every endpoint except health, signup, login and the GitHub
routes requires a valid `ember_session` cookie. There are no API keys and no
bearer tokens.

**Errors:** every failure returns

```json
{ "error": { "code": "MESSAGE_SAVE_FAILED", "message": "…", "retryable": true } }
```

| Status | Typical codes |
| --- | --- |
| 401 | `SESSION_EXPIRED`, `INVALID_CREDENTIALS` |
| 403 | `PERMISSION_DENIED`, `CHANNEL_ARCHIVED`, `NOT_A_CHANNEL_MEMBER`, `EMAIL_NOT_VERIFIED` |
| 404 | `CHANNEL_NOT_FOUND`, `CONVERSATION_NOT_FOUND`, `MESSAGE_NOT_FOUND` |
| 409 | `CONFLICT`, `HELP_REQUEST_INVALID_TRANSITION`, `EMAIL_ALREADY_REGISTERED` |
| 422 | `VALIDATION_FAILED` (includes `details.field`), `TOKEN_INVALID` (reset/verification link invalid or expired) |
| 429 | `LOGIN_RATE_LIMITED`, `SIGNUP_RATE_LIMITED`, `EMAIL_REQUEST_RATE_LIMITED` |
| 503 | `DATABASE_UNAVAILABLE` (retryable) |

**Pagination:** list endpoints take `limit` (1–100, default 50) and `offset`,
and return `{items, total, limit, offset, has_more}`.

---

## Health

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/health` | Liveness. No auth. |
| GET | `/api/health/ready` | Executes `SELECT 1`. No auth. |

## Auth — `/api/auth`

| Method | Path | Body / query | Notes |
| --- | --- | --- | --- |
| POST | `/signup` | `email`, `password`, `display_name` | 201. Creates a **member**. Sets a session cookie unless verification is required, in which case the response is neutral and no session is issued. |
| POST | `/login` | `email`, `password` | Generic failure message; rate limited. |
| POST | `/logout` | — | Revokes the session server-side and clears the cookie. |
| GET | `/session` | — | `{authenticated, user?, github_enabled}`. Safe when signed out. |
| GET | `/me` | — | The current user. Never includes hashes or tokens. |
| POST | `/password` | `current_password`, `new_password` | Revokes **all** sessions. |
| POST | `/password/set` | `new_password` | Adds a password to a GitHub-only account. |
| POST | `/password/forgot` | `email` | **Neutral** — never discloses whether the address exists. Emails a reset link. |
| POST | `/password/reset` | `token`, `new_password` | Consumes the token, sets the password, revokes all sessions. |
| POST | `/email/verify` | `token` | Confirms an address and signs the user in. |
| POST | `/email/resend` | — | Resend the confirmation email (auth required). |
| POST | `/sessions/revoke-all` | — | Signs out every device. |
| GET | `/github/start` | `redirect_to` (must start with `/`) | 302 to GitHub; stores a single-use state row. |
| GET | `/github/callback` | `code`, `state`, `error` | Completes sign-in; 302 back into Ember, or to `/signin?error=…`. |
| GET | `/github/status` | — | `{enabled: bool}`. |

## Profile — `/api/profile`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/profile` | Your profile, creating an empty one if missing. |
| PATCH | `/api/profile` | Any subset of `display_name`, `avatar_url`, `bio`, `current_project`, `project_area`, `working_status`, `available_to_help`, `skills[]`. Unsupplied fields are untouched. |
| PUT | `/api/profile/working-status` | `{working_status}`. |

## Members — `/api/members`

| Method | Path | Query |
| --- | --- | --- |
| GET | `/api/members` | `q`, `skill`, `working_status`, `available_only`, `project_area`, `include_self` |
| GET | `/api/members/skills` | — |
| GET | `/api/members/project-areas` | — |
| GET | `/api/members/{user_id}` | — |

Member payloads never contain email addresses.

## Channels — `/api/channels`

| Method | Path | Who | Notes |
| --- | --- | --- | --- |
| GET | `/api/channels` | member | `include_archived`, `only_archived`. Items carry `is_member` and `unread_count`. |
| POST | `/api/channels` | member | `{name, description?, topic?}` → 201. Any member may create. |
| GET | `/api/channels/{id}` | member | |
| PATCH | `/api/channels/{id}` | creator / admin | Rename/retopic. The slug never changes. |
| POST | `/api/channels/{id}/archive` | creator / admin | |
| POST | `/api/channels/{id}/restore` | creator / admin | |
| POST | `/api/channels/{id}/join` | member | 403 if archived. |
| POST | `/api/channels/{id}/leave` | member | |
| POST | `/api/channels/{id}/members` | creator / admin | `{user_id}` — invite (add) a member; they are notified. |
| DELETE | `/api/channels/{id}/members/{user_id}` | creator / admin | Remove a member. The creator cannot be removed. |
| POST | `/api/channels/{id}/invite-code` | creator / admin | Create/rotate the invite link → `{invite_code, invite_url}`. |
| DELETE | `/api/channels/{id}/invite-code` | creator / admin | Turn off the invite link. |
| POST | `/api/channels/join-by-code` | member | `{invite_code}` — join via a shared link. |
| GET | `/api/channels/{id}/members` | member | Paginated. |

## Messages — `/api/messages`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/messages/channel/{id}` | `before_seq`, `limit`. Returns `{items, has_older, oldest_seq, latest_seq}`; top-level messages only. |
| GET | `/api/messages/channel/{id}/new` | **Polling.** `after_seq`, `limit`. Returns only strictly newer messages. |
| POST | `/api/messages/channel/{id}` | `{body, parent_message_id?}` → 201. Requires membership and a non-archived channel. |
| GET | `/api/messages/{id}` | 404 for a DM you are not in. |
| PATCH | `/api/messages/{id}` | Author only, within 24 hours. |
| DELETE | `/api/messages/{id}` | Author or admin. Soft delete. |
| POST/DELETE | `/api/messages/{id}/pin` | channel creator / admin, channel messages only. |
| GET | `/api/messages/channel/{id}/pinned` | |
| PUT | `/api/messages/channel/{id}/read` | `{last_read_message_id?}`; defaults to the newest. |
| GET | `/api/messages/channel/{id}/unread` | `{unread_count}`. |

Each message includes server-computed `can_edit`, `can_delete`, `can_pin`,
`can_convert` and grouped `reactions`.

## Threads — `/api/threads`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/threads/{parent_id}` | Parent, replies, participants, `source_label`. Replies load only here. |
| POST | `/api/threads/{parent_id}/replies` | `{body}` → 201. Threads cannot nest. |

## Direct messages — `/api/direct-messages`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/direct-messages` | Your conversations with unread counts. |
| POST | `/api/direct-messages` | `{user_id}` → 201. Idempotent per pair. |
| GET | `/api/direct-messages/{id}/messages` | Paginated. **404 if you are not a participant.** |
| GET | `/api/direct-messages/{id}/messages/new` | Polling cursor. |
| POST | `/api/direct-messages/{id}/messages` | `{body, parent_message_id?}` → 201. |
| PUT | `/api/direct-messages/{id}/read` | Update your read position. |

## Reactions — `/api/reactions`

| Method | Path | Body |
| --- | --- | --- |
| POST | `/api/reactions/{message_id}` | `{reaction_type}` — idempotent |
| DELETE | `/api/reactions/{message_id}` | `{reaction_type}` |
| POST | `/api/reactions/{message_id}/toggle` | `{reaction_type}` |

Types: `thumbs_up`, `eyes`, `check`, `celebration`, `heart`.

## Notifications — `/api/notifications`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/notifications` | `unread_only`. Yours only. |
| GET | `/api/notifications/unread-count` | |
| POST | `/api/notifications/{id}/read` | 404 if it is not yours. |
| POST | `/api/notifications/read-all` | `{updated: n}`. |

## Announcements — `/api/announcements`

| Method | Path | Who |
| --- | --- | --- |
| GET | `/api/announcements` | member (`include_expired`) |
| POST | `/api/announcements` | **admin** |
| PATCH | `/api/announcements/{id}` | **admin** |
| DELETE | `/api/announcements/{id}` | **admin** |

## Help requests — `/api/help-requests`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/help-requests` | `status`, `category`, `urgency`, `assigned_to_me`, `created_by_me`, `unclaimed`, `q` |
| POST | `/api/help-requests` | `{title, description, category?, urgency?, source_message_id?}` |
| GET/PATCH | `/api/help-requests/{id}` | Edit: requester or admin |
| POST | `/api/help-requests/{id}/claim` | Anyone except the requester; must be `open` |
| POST | `/api/help-requests/{id}/unclaim` | Assigned helper or admin |
| POST | `/api/help-requests/{id}/resolve` | `{resolution_note?}` — requester, helper or admin |
| POST | `/api/help-requests/{id}/cancel` | Requester or admin |
| POST | `/api/help-requests/{id}/reopen` | Requester or admin |

## Decisions — `/api/decisions`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/decisions` | `q`, `status`, `channel_id`, `author_id`, `related_project` |
| POST | `/api/decisions` | `{title, decision_text, context?, related_project?, source_message_id?}` |
| GET/PATCH | `/api/decisions/{id}` | Editing requires `active` status |
| POST | `/api/decisions/{id}/supersede` | `{superseded_by_id}` — must be a different, active decision |
| POST | `/api/decisions/{id}/reverse` | `{reason?}` |

## Tasks — `/api/tasks`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/tasks` | `status`, `priority`, `assignee_id`, `creator_id`, `assigned_to_me`, `created_by_me`, `unassigned`, `q` |
| POST | `/api/tasks` | `{title, description?, assignee_id?, priority?, due_at?, source_message_id?}` |
| GET/PATCH | `/api/tasks/{id}` | PATCH: creator or admin |
| PUT | `/api/tasks/{id}/assignee` | `{assignee_id \| null}` — creator or admin |
| PUT | `/api/tasks/{id}/status` | `{status}` — assignee, creator or admin |

## Search — `/api/search`

`GET /api/search?q=…`

| Query | Meaning |
| --- | --- |
| `q` | **required**, 1–200 chars |
| `scope` | `all` (default), `messages`, `help_requests`, `decisions`, `announcements` |
| `channel_id`, `sender_id` | filters |
| `date_from`, `date_to` | ISO 8601 |
| `include_direct_messages` | default `true` — your own DMs only |
| `limit`, `offset` | capped at 100 |

Each result: `{kind, id, title, excerpt, source_label, link_path, author_name, created_at}`.

## Dashboard — `/api/dashboard`

One bounded summary: unread counts, up to three announcements, five open help
requests, your claimed requests, four recent decisions, five of your tasks,
five available helpers and five recent mentions. It never returns another
user's private data.

## Admin — `/api/admin`

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/api/admin/stats` | Record counts |
| GET | `/api/admin/users` | Paginated |
| PUT | `/api/admin/users/{id}/role` | `{role}` — audited |
| GET | `/api/admin/audit` | Recent audit events |

All four return 403 for members.

## HTML and HTMX routes

The UI is served from the same application: pages at `/`, `/channels`,
`/channels/{slug}`, `/dm`, `/dm/{id}`, `/threads/{id}`, `/help`, `/help/{id}`,
`/decisions`, `/decisions/{id}`, `/tasks`, `/tasks/{id}`, `/search`, `/members`,
`/members/{id}`, `/notifications`, `/announcements`, `/admin`, `/profile`,
`/signin`, `/signup`; HTMX fragments under `/hx/*`. They call the same service
functions as the JSON API, so the two can never disagree.
