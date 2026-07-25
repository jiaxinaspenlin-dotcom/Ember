# Ember

**Where cohort conversations turn into action.**

Ember is a communications platform for builders, students, hackathon teams,
accelerator cohorts and learning communities. It is not a Slack clone: alongside
channels, threads and direct messages, it gives a cohort the things that usually
get lost in a chat log — a **Help Queue**, a **Decision Log**, and **tasks with
real owners** — and lets any public message become one of them in two clicks.

---

## Table of contents

- [What Ember does](#what-ember-does)
- [Who it is for](#who-it-is-for)
- [Core features](#core-features)
- [Why the application is Python-first](#why-the-application-is-python-first)
- [Architecture](#architecture)
- [Prerequisites](#prerequisites)
- [Local setup](#local-setup)
- [PostgreSQL setup](#postgresql-setup)
- [Alembic migrations](#alembic-migrations)
- [Environment variables](#environment-variables)
- [GitHub OAuth setup](#github-oauth-setup)
- [Email authentication setup](#email-authentication-setup)
- [Forth integration](#forth-integration)
- [Making someone an administrator](#making-someone-an-administrator)
- [Rebuilding the stylesheet](#rebuilding-the-stylesheet)
- [Testing commands](#testing-commands)
- [Deployment](#deployment)
- [Empty-database behaviour](#empty-database-behaviour)
- [Verifying persistence](#verifying-persistence)
- [Known limitations](#known-limitations)
- [Documentation index](#documentation-index)

---

## What Ember does

A cohort talks in public channels and direct messages. Ember keeps that
conversation, and adds the follow-through:

| Someone says… | Ember turns it into… |
| --- | --- |
| "I'm blocked on the deploy" | a **help request** someone can claim and resolve |
| "OK, we're going with Postgres" | a **decision** in a searchable log, with context |
| "Can you write the onboarding email?" | a **task** with an assignee, priority and due date |
| "Here's the handbook link" | a **pinned resource** at the top of the channel |
| "Would love feedback on this" | a **feedback request** in the Help Queue |

Every one of those is a real, persisted record with its own permissions, state
machine and audit trail.

## Who it is for

- **Accelerator and bootcamp cohorts** who need decisions to survive the program
- **Hackathon teams** who need to know who is free to help, right now
- **Learning communities** where the answer to a question should be findable in
  six weeks' time
- **Any small group (~30 people)** that wants conversation to produce action

## Core features

- **Cohorts (multi-tenancy)** — every workspace is a cohort with its own
  channels, members and admins; one person can belong to many and switch between
  them. Data is isolated in SQL on every read, so nothing leaks between cohorts.
  Open-join for the demo, invite links when shipped. See
  [docs/COHORTS.md](docs/COHORTS.md).
- **Public channels** — any member can create one and becomes that channel's
  admin: they can invite or remove members and share a one-click invite link
  (cohort admins manage any channel in their cohort). Joinable by members;
  archivable (archived channels stay readable and searchable, but reject writes)
- **Direct messages** — private one-to-one conversations, invisible to everyone
  else including their existence
- **Threads** — self-referencing replies, loaded only when a thread is opened
- **Reactions** — 👍 👀 ✅ 🎉 ❤️, one per user per type per message, enforced by a
  database constraint
- **Mentions** — `@person`, `@channel`, `@admins`, parsed in Python and filtered
  through the same access rules as everything else
- **Notifications** — private and persisted, for mentions, DMs, thread replies,
  assignments, help-request events, announcements and decision changes
- **Unread tracking** — read receipts in PostgreSQL, counts computed in SQL, so
  they survive logging in on another device
- **Near-real-time updates** — HTMX polls a cursor endpoint every 4 seconds and
  receives *only* messages newer than what it already has
- **Help Queue** — open / claimed / resolved / cancelled, with categories,
  urgency and filters
- **Decision Log** — active / superseded / reversed, full-text searchable,
  never destructive
- **Tasks** — to do / in progress / blocked / done, with creator-and-admin
  management and assignee status control
- **Member directory** — skills, current project, project area, working status
  and availability; email addresses are never exposed
- **Online presence** — a green/amber/grey dot per member (online / away /
  offline), from a throttled last-active heartbeat, plus "N online now"
- **Kudos** — public shout-outs from one member to another, with a cohort wall
  and a notification to the recipient
- **Daily check-ins** — a "what I'm working on today" feed that also refreshes
  the member's current project
- **Cohort campfire** — the home page shows a fire that grows with the cohort's
  recent momentum (messages, decisions, resolved help, completed tasks, kudos,
  check-ins, new members), scored over a rolling window
- **Announcements** — cohort-admin-only, with priority and pinning
- **Search** — PostgreSQL full-text search across messages, help requests,
  decisions and announcements, cohort-scoped and permission-filtered inside the
  SQL query
- **Cohort admin console** — per-cohort roles, channel management, the invite
  link, statistics and an audit trail, all fenced to one cohort
- **Forth integration (link-only)** — attach a cohort's Forth project-management
  workspace and link Forth items from tasks, decisions, help requests and
  messages; validated server-side, no shared accounts or data. See below.
- **Account recovery** — email verification (opt-in) and password reset, with
  non-disclosing responses

## Why the application is Python-first

Every rule that matters lives in Python, and the browser is never asked to
enforce anything:

- **Authentication, sessions and OAuth** are owned by FastAPI. Sessions are
  opaque random tokens; only their SHA-256 hash is stored.
- **Authorization** lives in one module (`app/auth/permissions.py`). Routes call
  it; templates receive pre-computed boolean flags. A control that a user is not
  allowed to use is never rendered — and the route rejects it anyway.
- **Unread counts, search results, state transitions and mention resolution**
  are computed in Python or SQL and handed to the page as finished values.
- **The frontend is server-rendered HTML.** There is no parallel TypeScript data
  layer that could drift into holding business logic.

The JavaScript in this project (`backend/app/static/js/ember.js`, ~120 lines)
handles scrolling, the Enter-to-send shortcut, and surfacing server errors.
That is all it does.

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Browser                                                 │
│  server-rendered HTML · HTMX (polling, fragments)        │
│  Alpine.js (menus/tabs only) · Tailwind CSS · Inter      │
└───────────────────────────┬──────────────────────────────┘
                            │  same-origin, HTTP-only cookie
┌───────────────────────────▼──────────────────────────────┐
│  FastAPI (one Python service)                            │
│                                                          │
│  app/web/routes/     HTML pages + HTMX fragments         │
│  app/api/routes/     JSON API (/api/*)                   │
│         ↓ both call ↓                                    │
│  app/services/       ALL business logic                  │
│  app/auth/           sessions · passwords · permissions  │
│  app/search/         permission-aware full-text search   │
│  app/models/         SQLAlchemy 2 ORM                    │
└───────────────────────────┬──────────────────────────────┘
                            │  psycopg 3
┌───────────────────────────▼──────────────────────────────┐
│  PostgreSQL (Neon in production)                         │
│  24 tables · constraints · indexes · tsvector search     │
└──────────────────────────────────────────────────────────┘
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the reasoning, including why
Jinja2 + HTMX was chosen over Next.js.

## Prerequisites

- **Python 3.12+**
- **PostgreSQL 14+** (local for development, hosted for production)
- **Node 18+** — *only* if you want to rebuild the Tailwind stylesheet; the
  compiled CSS is committed, so running the app never needs Node.

## Local setup

```bash
git clone <your-fork> ember
cd ember/backend

python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

cp .env.example .env
# Generate a session secret and paste it into .env:
python -c "import secrets; print(secrets.token_urlsafe(48))"

# Point DATABASE_URL at your local PostgreSQL, then:
alembic upgrade head

uvicorn app.main:app --reload
```

Open <http://localhost:8000>. The database is empty: create the first account
through the sign-up form.

## PostgreSQL setup

**macOS (Homebrew):**

```bash
brew install postgresql@16
brew services start postgresql@16
createdb ember_dev
createdb ember_test
```

**Docker:**

```bash
docker run --name ember-postgres -e POSTGRES_PASSWORD=ember \
  -e POSTGRES_DB=ember_dev -p 5432:5432 -d postgres:16
```

Then set in `.env`:

```
DATABASE_URL=postgresql+psycopg://USER@localhost:5432/ember_dev
```

SQLite is not supported. `DATABASE_URL` is validated at startup and the
application refuses to run against anything other than PostgreSQL.

## Alembic migrations

```bash
cd backend

alembic upgrade head            # apply all migrations
alembic downgrade base          # roll everything back
alembic check                   # confirm models and migrations agree
alembic revision --autogenerate -m "describe the change"
```

Migrations are the *only* thing that touches the schema, and they never insert
data. `alembic check` is part of the review checklist — it currently reports
"No new upgrade operations detected."

## Environment variables

Full reference: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md). The essentials:

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL connection string (required) |
| `SESSION_SECRET` | Signing/encryption secret (required, ≥16 chars) |
| `FRONTEND_URL` / `BACKEND_URL` | Public URLs; identical in a single-service deploy |
| `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` | GitHub OAuth app credentials |
| `GITHUB_OAUTH_REDIRECT_URI` | Must match the GitHub app exactly |
| `SESSION_COOKIE_NAME` | Cookie name (default `ember_session`) |
| `SESSION_MAX_AGE_DAYS` | Session lifetime (default 30) |
| `POLLING_INTERVAL_MS` | Client poll interval (default 4000) |
| `COHORT_OPEN_JOIN` | `true` (demo): any cohort is discoverable and joinable. `false` (shipped): joining needs an invite link |
| `MAX_COHORTS_CREATED_PER_USER` | How many cohorts one account may create (default 10; `0` disables the cap) |
| `TRUST_PROXY_HEADERS` | Honour `X-Forwarded-For` (set `true` behind Render/Fly) |
| `EMAIL_BACKEND` / `SMTP_*` | Email delivery (`auto` uses SMTP when set, else logs) |
| `REQUIRE_EMAIL_VERIFICATION` | Gate new email/password accounts until confirmed |
| `LOGIN_MAX_ATTEMPTS` / `LOGIN_MAX_ATTEMPTS_PER_IP` | Independent rate-limit budgets |
| `ENVIRONMENT` | `development`, `test` or `production` |

`.env`, `.env.local` and every secret are git-ignored. Only `.env.example` is
committed, and it contains no real values.

## GitHub OAuth setup

Create an OAuth app at <https://github.com/settings/developers>.

**Local:**

- Homepage URL: `http://localhost:8000`
- Authorization callback URL: `http://localhost:8000/api/auth/github/callback`

**Production:**

- Homepage URL: `https://YOUR-DOMAIN`
- Authorization callback URL: `https://YOUR-DOMAIN/api/auth/github/callback`

The callback URL must match `GITHUB_OAUTH_REDIRECT_URI` **exactly**, including
scheme, host, port and path. Ember requests only `read:user user:email` — never
repository, organization or write scopes.

If `GITHUB_CLIENT_ID`/`GITHUB_CLIENT_SECRET` are unset, the GitHub button is
simply not rendered and email/password sign-in works normally.

Details: [docs/GITHUB_OAUTH.md](docs/GITHUB_OAUTH.md).

## Email authentication setup

No configuration needed — email/password sign-up works out of the box:

- emails are normalised and unique
- passwords are hashed with **Argon2** (`pwdlib`), minimum 10 characters with a
  letter and a digit
- **password reset by email** works end to end (neutral responses, single-use
  hash-at-rest tokens, session revocation on completion)
- **email verification** is available opt-in (`REQUIRE_EMAIL_VERIFICATION=true`)
- verification is constant-time, and a missing account still performs a full
  hash comparison so timing cannot reveal whether an address is registered
- failed sign-ins are rate limited (8 attempts per 15 minutes, per email and per
  IP, tracked in PostgreSQL so the limit survives restarts)
- failure messages are deliberately identical for wrong password and unknown
  account

Email delivery powers password reset and verification. With SMTP unset, mail is
written to the application log (the "console" backend) so development is honest —
nothing is faked. See [docs/EMAIL.md](docs/EMAIL.md).

## Forth integration

Ember has a deliberately **lightweight, link-only** integration with
[Forth](https://forth-bice.vercel.app), the cohort project-management platform.

**What it currently does**

- A cohort **admin** can attach the cohort's Forth workspace URL (Admin console →
  *Forth workspace*). When set, an **“Open Forth”** button appears in the sidebar
  for every member, opening Forth in a new tab with `rel="noopener noreferrer"`.
- **Tasks, decisions, and help requests** can each carry an optional Forth link,
  shown as a labelled **“View in Forth”** link on the item.
- **Messages** that contain a Forth link render a minimal **link-preview card**
  (provider label, the safe path, and an “Open in Forth” link).
- Every Forth URL is validated **server-side** using parsed URL components (not
  string matching): it must use **`https`** and have the exact host
  **`forth-bice.vercel.app`**. Lookalike domains, userinfo tricks, and unsafe
  schemes (`javascript:`, `data:`, …) are rejected. Only cohort admins may set the
  workspace URL, and only within their own cohort.

**What it does not do**

- It does **not** call any Forth API, fetch task names/statuses/assignees, or
  display any Forth data — Forth exposes no confirmed external task API,
  integration-token flow, or webhook system, so Ember invents none of it.
- It does **not** share authentication. Ember and Forth **retain entirely
  separate accounts and databases**, and Ember uses a different Firebase project
  than Forth (in fact Ember uses no Firebase at all). No Firebase credentials,
  OAuth tokens, or Forth passwords are ever stored.
- The link-preview cards never reach out to Forth; they only reformat the URL a
  member typed.

**What deeper integration would require**

Real synchronization — showing live Forth task status inside Ember, creating
Forth tasks from Ember, or reacting to Forth changes — would require Forth to
expose an **authenticated task API** plus an **integration-token flow** and a
**webhook system**. None of those exist today, so they are intentionally out of
scope.

## Making someone an administrator

Admin is **per-cohort** — there is no global installation admin. The person who
creates a cohort is its admin; from there, roles change through two server-side
paths only:

1. **Cohort admin console** — an existing admin changes a member's role at
   `/admin`. The change is authenticated, authorized and written to the audit
   log. A cohort always keeps at least one admin (the last admin can't be
   demoted).

2. **CLI** (from `backend/`, with the venv active) — role commands name the
   cohort, since roles are per-cohort:

   ```bash
   ember-admin list-users
   ember-admin grant-admin  --email someone@example.com --cohort summer-2026
   ember-admin grant-admin  --github their-username      --cohort summer-2026
   ember-admin revoke-admin --email someone@example.com --cohort summer-2026
   ```

Roles can never be set through a signup payload, profile edit, URL parameter or
browser storage — there are tests asserting exactly that. Full model:
[docs/COHORTS.md](docs/COHORTS.md).

## Rebuilding the stylesheet

```bash
npm install          # once, at the repository root
npm run css          # rebuild backend/app/static/css/app.css
npm run css:watch    # rebuild on change while developing
```

The output is committed. Production never runs Node.

## Testing commands

```bash
cd backend
source .venv/bin/activate

createdb ember_test                       # once
pytest -q                                 # 250 tests
pytest app/tests/test_permissions.py -v   # one suite
ruff check .                              # lint
mypy app                                  # strict type checking
alembic check                             # models match migrations
```

Tests run against a **real PostgreSQL database** (`ember_test`) so constraints,
indexes and full-text search are genuinely exercised. Every test starts with a
truncated, completely empty database and builds whatever it needs through the
real service layer. Override the target with `TEST_DATABASE_URL`.

More detail: [docs/TESTING.md](docs/TESTING.md).

## Deployment

Ember deploys as **one Python service** plus **hosted PostgreSQL**.

1. Create a database (Neon recommended) and copy the **pooled** connection
   string.
2. Deploy `backend/` to Render, Fly.io or Railway.
   - `render.yaml` and `fly.toml` are included and ready.
   - Migrations run as a release/pre-deploy step (`alembic upgrade head`), never
     at import time.
3. Set the environment variables above, with `ENVIRONMENT=production` (this
   turns on `Secure` cookies and HSTS, and disables the API docs). The app
   **refuses to boot** in production with a placeholder `SESSION_SECRET` or a
   non-https URL, so a misconfigured deploy fails loudly instead of quietly.
4. Update the GitHub OAuth app's callback URL to your production domain.

Because the UI and the API share an origin, there is no CORS configuration and
no cross-domain cookie problem to solve.

Full runbook: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Empty-database behaviour

A fresh installation contains **zero** users, channels, messages, conversations,
tasks, decisions, help requests and notifications. Nothing is seeded — not at
startup, not during migrations, not on signup, not on page load.

Every page has a designed empty state:

| Page | Empty state |
| --- | --- |
| Channels | "No channels yet." — admins see *Create the first channel* |
| A channel | "No messages yet. Start the conversation." |
| Direct messages | "No conversations yet. Message a cohort member." |
| Help Queue | "No help requests are open." |
| Decision Log | "No decisions have been recorded." |
| Tasks | "No tasks are assigned to you." |
| Notifications | "You're all caught up." |
| Members | "No other members have joined yet." |

Empty states are visually distinct from error states (dashed neutral panel
versus a red error panel) and are asserted by tests.

## Verifying persistence

```bash
# 1. Create an account and post a message through the UI at localhost:8000
# 2. Restart the backend entirely
#    (Ctrl-C, then `uvicorn app.main:app --reload` again)
# 3. Reload the page — the message is still there.
# 4. Confirm directly in the database:
psql -d ember_dev -c "select count(*) from users;"
psql -d ember_dev -c "select body, created_at from messages order by seq desc limit 5;"
psql -d ember_dev -c "select action, created_at from audit_events order by created_at desc limit 5;"
# 5. Sign in from a different browser: the same data, and the same unread counts.
```

Nothing is stored in React state, `localStorage` or process memory.
`localStorage` is not used at all in this build.

## Known limitations

Honest list, with reasons:
[docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md).

## Documentation index

| Document | Contents |
| --- | --- |
| [docs/SYSTEM_OVERVIEW.md](docs/SYSTEM_OVERVIEW.md) | What Ember is and how the pieces fit |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Frontend decision, layering, request lifecycle |
| [docs/PYTHON_BACKEND.md](docs/PYTHON_BACKEND.md) | Module-by-module tour of the backend |
| [docs/API_REFERENCE.md](docs/API_REFERENCE.md) | Every endpoint, request and response |
| [docs/DATABASE_SCHEMA.md](docs/DATABASE_SCHEMA.md) | Tables, constraints, indexes |
| [docs/AUTHENTICATION.md](docs/AUTHENTICATION.md) | Signup, login, sessions, password policy |
| [docs/GITHUB_OAUTH.md](docs/GITHUB_OAUTH.md) | The full OAuth flow and account linking |
| [docs/PERMISSIONS_AND_PRIVACY.md](docs/PERMISSIONS_AND_PRIVACY.md) | Every permission boundary |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, headers, rate limiting, accepted risks |
| [docs/EMAIL.md](docs/EMAIL.md) | Password reset, email verification, SMTP setup |
| [docs/PERSISTENCE.md](docs/PERSISTENCE.md) | What is stored, and the transaction model |
| [docs/POLLING.md](docs/POLLING.md) | The cursor design and its cost |
| [docs/HELP_QUEUE.md](docs/HELP_QUEUE.md) | Help-request state machine |
| [docs/DECISION_LOG.md](docs/DECISION_LOG.md) | Decision state machine |
| [docs/TASKS.md](docs/TASKS.md) | Task permissions and statuses |
| [docs/TESTING.md](docs/TESTING.md) | Test strategy and coverage map |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Production runbook |
| [docs/KNOWN_LIMITATIONS.md](docs/KNOWN_LIMITATIONS.md) | What is deliberately not built |

## License

MIT.
