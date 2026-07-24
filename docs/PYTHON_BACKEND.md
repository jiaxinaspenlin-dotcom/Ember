# The Python backend

A module-by-module tour. Everything that decides anything lives here.

## `app/core`

| Module | Contents |
| --- | --- |
| `config.py` | `Settings` (pydantic-settings). Validates `DATABASE_URL` is PostgreSQL and normalises the driver to `psycopg`. Exposes derived properties: `admin_email_set`, `cookie_secure`, `github_oauth_configured`. |
| `enums.py` | Permitted system states — roles, working statuses, message/reaction/mention/notification types, priorities, help categories and all three status machines. These are code-level enums; no record is ever defined here. |
| `errors.py` | `EmberError` and subclasses, each with `code`, `status_code`, `retryable` and `to_dict()` producing the documented envelope. |
| `logging.py` | `scrub()` redacts passwords, tokens, cookies and message bodies from any structured context before it is logged. |
| `security.py` | Opaque token generation, SHA-256 token hashing, constant-time compare, Fernet encryption for provider tokens, email normalisation. |

## `app/db`

- `base.py` — `Base` with a naming convention (so every constraint and index has
  a predictable name), `TimestampMixin`, `utcnow()`, and `rows_affected()`.
- `session.py` — engine with `pool_pre_ping=True` (important against Neon's
  connection recycling), `pool_size=10`, `max_overflow=20`,
  `expire_on_commit=False`, plus `get_db()` and `session_scope()`.

## `app/models`

24 tables across `user.py`, `channel.py`, `message.py`, `action.py` and
`engagement.py` (`user.py` also holds `EmailToken`, backing password reset and
verification). Typed `Mapped[...]` throughout. See
[DATABASE_SCHEMA.md](DATABASE_SCHEMA.md).

Two details worth calling out:

- `Message.seq` is backed by a real PostgreSQL sequence (`message_seq`), giving
  every message a total order across the installation. The polling cursor and
  every unread count are a single indexed comparison against it.
- `Message.search_vector` and `Decision.search_vector` are `GENERATED ALWAYS AS
  … STORED` columns with GIN indexes, so search stays correct without triggers
  or application-side maintenance.

## `app/auth`

| Module | Responsibility |
| --- | --- |
| `passwords.py` | Argon2 hashing via `pwdlib`; the policy; a dummy-hash path so verification is constant-time when no credential exists. |
| `sessions.py` | Create / resolve / revoke sessions. Only the token *hash* is stored. `last_seen_at` is refreshed at most every 5 minutes to avoid a write per request. |
| `github.py` | The real OAuth flow — DB-stored single-use `state`, code exchange, identity fetch, primary-verified-email selection. |
| `permissions.py` | **The** authorization module. Every `can_*` / `require_*` predicate in the system. |

`permissions.py` is intentionally the only place that answers "may they?". It has
no HTTP dependencies, so it is directly unit-testable, and
`channel_capabilities()` produces the flag dictionary templates render from.

## `app/services`

| Module | Highlights |
| --- | --- |
| `accounts.py` | Registration (`register_account` applies the verification policy), password auth with DB-backed rate limiting, GitHub identity resolution and the account-linking rules, `initial_role_for()`, `set_user_role()`. |
| `profiles.py` | Profile updates (targeted — only supplied fields change), skill find-or-create, the member directory query and its filters. |
| `channels.py` | Channel CRUD, membership, and `list_channels()` which returns channels *with* per-user membership and unread counts in one query. |
| `messages.py` | Create/edit/soft-delete/pin, cursor pagination, the polling query, threads, reactions, read receipts and all unread arithmetic. |
| `mentions.py` | Pure `extract_handles()` plus DB-backed resolution filtered through the destination's real audience. |
| `direct_messages.py` | Canonical `pair_key` conversation lookup, conversation list with unread counts. |
| `notifications.py` | Create (skipping self-notifications), list, unread count, mark read / all read. |
| `announcements.py` | Admin-only create/update/delete, fan-out notification. |
| `help_requests.py` | The help state machine and the Help Queue query. |
| `decisions.py` | The decision state machine, supersede/reverse, log filters. |
| `tasks.py` | Task creation, assignment, status transitions, filters. |
| `dashboard.py` | Assembles the home summary from bounded queries. |
| `credentials.py` | Email verification and password reset: single-use hash-at-rest tokens, neutral responses, session revocation. |
| `audit.py` | Append-only audit rows, written in the same transaction as the action. |

Every mutating function is targeted — `rename_channel`, `archive_channel`,
`claim_help_request`, `update_task_status`. Nothing deletes and recreates a
related graph. `profiles._replace_skills()` is the most complex case and it
still adds and removes only the links that actually changed.

## `app/search`

`queries.py` builds the search statement with the permission filter **inside**
the SQL:

```python
dm_visible = Message.direct_conversation_id.in_(
    select(DirectConversationMember.conversation_id)
    .where(DirectConversationMember.user_id == user_id)
)
```

A user cannot receive a DM they are not part of, because the database never
returns one. Results are capped at 100 and paginated.

## `app/api/routes` and `app/web/routes`

Two thin layers over the same services.

- `api/` returns Pydantic models; `api/dependencies.py` provides `DbDep`,
  `AuthDep` (401 on failure), `AdminUser` (403 for members) and `PaginationDep`.
- `web/` returns HTML; `web/deps.py` provides `PageAuth`, which raises
  `PageRedirect` so an unauthenticated *page* request lands on
  `/signin?next=…` instead of a bare 401.

Routes under `/hx/*` return fragments for HTMX.

## `app/cli.py`

`ember-admin` — `list-users`, `grant-admin`, `revoke-admin`, `stats`,
`purge-sessions`. This and the authenticated `/admin` console are the only ways
a role can change.
