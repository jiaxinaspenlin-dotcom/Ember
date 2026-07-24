# Testing

**271 tests**, all Python, all against a real PostgreSQL database.

```bash
cd backend
source .venv/bin/activate
createdb ember_test                # once
pytest -q                          # whole suite (~40 s)
pytest app/tests/test_permissions.py -v
ruff check .                       # lint
mypy app                           # strict type checking, 87 files
alembic check                      # models and migrations agree
```

## Why a real database

SQLite would not exercise what Ember actually relies on: `tsvector` full-text
search, GIN indexes, partial and composite unique constraints, CHECK constraints,
`GENERATED ALWAYS AS STORED` columns, and a real sequence for message ordering.
Tests that pass against a substitute database prove less than the constraints
they skip.

Target `TEST_DATABASE_URL` (default
`postgresql+psycopg://localhost:5432/ember_test`).

## Isolation, and the no-seed-data rule

```python
@pytest.fixture(autouse=True)
def _empty_database():
    TRUNCATE <every table> RESTART IDENTITY CASCADE
```

Every test starts with **zero rows in every table**. There are no fixtures that
insert application content. When a test needs a user or a channel it creates one
through the real service layer (`make_user`, `make_channel`), so the test
exercises registration and channel creation rather than bypassing them.

Nothing in the test suite ever writes to a production database.

## Coverage map

| File | Tests | Covers |
| --- | --- | --- |
| `test_auth.py` | 35 | Signup, Argon2 hashing, password policy, login, generic failure messages, account enumeration, rate limiting, session storage (hash-only), logout, expiry, multi-device, GitHub first/returning login, missing and unverified email, no-overwrite of edited fields, token handling, OAuth state single-use, default member role, role-injection attempts |
| `test_permissions.py` | 22 | Anonymous blocked, member vs admin, channel membership, archived read-only, DM privacy (404 not 403), search boundaries, notification privacy, message edit/delete/pin rules, DM non-convertibility, task permissions, admin endpoints, audited role change |
| `test_messaging.py` | 35 | Channel CRUD and slugs, join/leave, message persistence and ordering, validation, edit, soft delete, pagination, polling (only newer, no history replay, no thread replies), threads, reactions (uniqueness at service *and* database level, toggle, summary), mentions (user/@channel/@admins, unauthorized never notified, pure parser), pins, unread counts, read receipts, cross-device unread, DM uniqueness and listing |
| `test_actions.py` | 39 | Help-request creation from a message, claim/unclaim/resolve/cancel/reopen, permission rules, illegal transitions, parameterised transition tables, queue filters, feedback category; decision creation, supersede (including self- and double-supersede rejection), reverse, author notification, log filters; task creation, assignment, status/`completed_at`, filters, counts; announcements and notification fan-out |
| `test_search_and_dashboard.py` | 17 | Query normalisation, excerpts, keyword search, deleted-content exclusion, channel/sender/date filters, cross-type search, result limits and pagination, DM inclusion only for participants, empty dashboard, populated dashboard, dashboard privacy, directory filters, no email exposure |
| `test_persistence.py` | 12 | Data visible from a brand-new session, survives logout and re-login in another browser, session rows, read receipts, notifications, action state, soft deletes, audit events, audit scrubbing, rollback on constraint violation, targeted edits not recreating related rows |
| `test_empty_and_errors.py` | 21 | Zero-row start, no records created at startup or signup, valid empty list responses, empty dashboard and search, every page's empty state, empty ≠ error, sign-in redirects, post-logout inaccessibility, the error envelope, validation/404/401/409 distinctions, HTMX inline error banner, failed write returns an error rather than pretending to succeed |
| `test_email_flows.py` | 20 | Password reset end to end (neutral responses, hash-at-rest, single-use, session revocation, weak-password rejection, GitHub-only guidance, per-IP limit); email verification opt-in (unconfirmed account, the gate, confirmation, non-disclosure of existing addresses, single-use, address-change invalidation); every rendered page |
| `test_github_oauth_flow.py` | 7 | The full OAuth handshake against a mocked transport — authorize redirect, DB-stored single-use state, code exchange, identity fetch, first-login account creation, returning-login reuse, verified-email linking, replayed/unknown state, cancellation, token encryption |
| `test_hardening.py` | 20 | Security headers and CSP clauses, production config guard (placeholder secret, short secret, non-https URLs, SQLite rejection), independent per-account and per-IP rate-limit budgets, signup throttling, proxy-header trust, oversized-input caps, admin self-demotion blocked on every path |
| `test_channel_invites.py` | 17 | Per-channel admin (creator) inviting and removing members, idempotent invites, notification on invite, the creator being unremovable, invite-link generation / rotation / revocation, join-by-link, invite codes never exposed in payloads, and the full flow through both the API and the web pages |
| `test_web_flows.py` | 22 | Full signup → profile → home journey, duplicate-email error page, channel creation and posting, join prompt, archived read-only page, polling fragment, reaction toggle, thread page and reply, DM journey, the whole message-to-action menu (help, decision, task, pin, feedback), help claim/resolve via UI, decision supersede via UI, inline task status, notifications page and badge, search page, member directory, admin console gating, self-demotion refusal, announcements, HTML escaping, accessibility landmarks |

## Pure-logic tests

Testable without HTTP or fixtures, exactly as the brief asks:

- **Mention parsing** — `extract_handles`, `normalize_handle`
- **Unread counts** — SQL aggregates driven directly through the service
- **Help-request transitions** — parameterised over the transition table
- **Decision transitions** — same
- **Task transitions** — `completed_at` set/clear behaviour
- **Permission predicates** — called directly on real model instances
- **Search query construction** — `normalize_query`, `build_excerpt`
- **Notification generation** — asserted per recipient after each action

## Error-path tests

Failed commits and rollback · validation errors with a field · expired sessions ·
unauthorized access · retryable vs non-retryable codes · HTMX inline errors ·
archived-channel writes rejected with nothing written.

## Frontend testing

The UI is server-rendered, so it is tested by asserting on the real rendered
HTML through the real routes (`test_web_flows.py`, plus the page assertions in
`test_empty_and_errors.py`): empty states, permission-dependent controls, HTMX
fragment behaviour, HTML escaping and accessibility landmarks. This covers
critical rendering and interaction **without duplicating backend business
logic** — the tests check what was rendered, never re-derive whether it should
have been.

There is no separate JavaScript test suite: `ember.js` contains no business
logic (scrolling, a keyboard shortcut, error toasts).

## Type checking and linting

- **mypy** runs in `strict` mode over all 92 source files, including the
  pydantic plugin. Clean.
- **ruff** with `E, F, I, UP, B, C4, SIM, RUF`. Clean.
- **alembic check** confirms the migration matches the models exactly.

## What is not covered

- The live HTTP calls to `github.com` are the only thing stubbed in the OAuth
  tests — a mock transport plays GitHub's role while the real backend code runs
  (`test_github_oauth_flow.py`). Verifying against the real github.com is a
  manual step — see [GITHUB_OAUTH.md](GITHUB_OAUTH.md#manual-verification).
- Real SMTP delivery. Email tests run on the console backend and capture
  outbound mail; sending through a live provider is a manual check.
- Browser rendering (no headless browser in the suite).
- Load testing beyond the ~30-member design target.
