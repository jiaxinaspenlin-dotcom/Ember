# Cohorts (Multi-Tenancy)

Ember is multi-tenant. A **cohort** is a workspace — its own channels, direct
messages, help queue, decision log, tasks, announcements, members and admins.
One person can belong to many cohorts, and everything they see is scoped to the
**one cohort that is currently active** in their session.

This is the Slack model: one identity, many workspaces, hard walls between them.

---

## The two layers of identity

Identity is **global**; everything else is **per-cohort**.

| Global (the `User`) | Per-cohort (the `CohortMembership`) |
| --- | --- |
| email, password, GitHub link | role (`member` / `admin`) |
| display name, avatar | bio, current project, project area |
| email-verified flag | skills, working status, available-to-help |
| — | profile-completed flag |

A `User` row is the person. A `CohortMembership` row is that person *inside one
cohort*. Joining a cohort creates a membership; leaving deletes it. Your display
name and avatar follow you everywhere; your bio, skills and role are set fresh in
each cohort.

See [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) for the columns and constraints.

---

## How isolation is enforced

Every tenant-owned table carries a non-null `cohort_id`
(`channels`, `messages`, `direct_conversations`, `help_requests`, `decisions`,
`tasks`, `announcements`, `notifications`, `cohort_memberships`). Isolation is
enforced **in SQL on every read**, not in the UI:

- Every `get_*` checks `row.cohort_id == actor.cohort_id`, and a mismatch raises
  **404 — the resource simply does not exist for you**. A member of cohort B who
  guesses a cohort-A channel id gets `CHANNEL_NOT_FOUND`, never a 403 (a 403
  would confirm the row exists).
- Every `list_*` filters `WHERE cohort_id = :active_cohort`.
- Cross-cohort actions (posting into another cohort's channel, DMing a non-member)
  are refused.

The wall is covered end to end by
[`app/tests/test_cohort_isolation.py`](../backend/app/tests/test_cohort_isolation.py):
service-layer 404s, scoped listings, cross-cohort DM/post refusals, API probes
by id, and the workspace switch changing what you see.

---

## The active cohort & the workspace switcher

The active cohort lives on the session (`sessions.active_cohort_id`).

- **One cohort** → it is auto-selected; the user never has to choose.
- **Several cohorts** → the last-used one is remembered. The sidebar shows a
  **workspace switcher** (`POST /cohorts/{slug}/switch`) to move between them.
- **No cohort** → the request is answered with **409 `NO_ACTIVE_COHORT`** on the
  API, and web pages **redirect to `/cohorts`**, the "create or join a cohort"
  picker.

Requests resolve their cohort through the `CohortContext` dependency
(`user`, `session`, `cohort`, `member`); web pages use `PageCohort`. Admin-only
endpoints use `AdminCohortDep`, which additionally requires
`member.is_admin` **in the active cohort**.

---

## Joining a cohort: open-join vs. invite link

Discovery is governed by the `COHORT_OPEN_JOIN` flag (`cohort_open_join`).

| `COHORT_OPEN_JOIN` | Behaviour | Intended for |
| --- | --- | --- |
| `true` (default) | Every cohort is **discoverable**. Anyone signed in can find and join any cohort from the picker. | **The demo** — no invites needed. |
| `false` | Cohorts are **not listed**. Joining requires a **cohort invite link** (`/join/{invite_code}`). | **Shipped** deployments. |

Either way, a cohort admin can always generate a shareable invite link from the
admin console, so people can join a specific cohort directly regardless of the
flag. `create-or-join` is the landing page for anyone without a cohort.

> Channels have their own, separate invite links (`/channels/join/{code}`) for
> inviting an existing cohort member into a specific channel. Don't confuse the
> two: `/join/{code}` joins a **cohort**; `/channels/join/{code}` joins a
> **channel** within your current cohort.

---

## Admin, fenced to one cohort

There is **no global installation admin** any more. Admin is **per-cohort**:

- The **creator of a cohort** is automatically its admin.
- A cohort admin can do inside their cohort what the old global admin could do
  across the whole install — manage channels, change members' roles, publish
  announcements, see the audit log, manage the invite link — **and nothing
  outside it**. A bad actor who spins up their own cohort is admin of their own
  sandbox and cannot see or touch anyone else's.
- A cohort must always keep **at least one admin**: the last admin cannot be
  demoted or leave (`COHORT_LAST_ADMIN`).

`/admin` is the **cohort admin console**, scoped to the active cohort.

### Platform operations: the `ember-admin` CLI

Truly installation-wide operations live only in the `ember-admin` CLI, never on
any HTTP surface. Because roles are per-cohort, role commands take a cohort:

```
ember-admin list-users
ember-admin grant-admin  --email someone@example.com --cohort summer-2026
ember-admin revoke-admin --github someuser          --cohort summer-2026
ember-admin stats
ember-admin purge-sessions
```

Role changes happen **only** here or through the authenticated cohort-admin API —
never from a request payload, URL parameter, or browser state. See
[PERMISSIONS_AND_PRIVACY.md](PERMISSIONS_AND_PRIVACY.md).

---

## Lifecycle summary

1. **Sign up / sign in** — creates or authenticates the global `User`.
2. **No cohort yet** → land on `/cohorts` to **create** one (you become its
   admin) or **join** one (open-join or invite link).
3. **Complete your profile** — per-cohort (`/profile/complete`).
4. **Work** — everything you see and do is scoped to the active cohort.
5. **Switch** — jump between your cohorts from the sidebar switcher.
6. **Leave** — deletes your membership (unless you are the last admin).
