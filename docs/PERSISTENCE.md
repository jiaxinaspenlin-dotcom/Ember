# Persistence

Every user-created record lives in PostgreSQL. Nothing that matters is stored in
browser state, process memory, or the filesystem.

## What is persisted

| Category | Tables |
| --- | --- |
| Accounts | `users`, `oauth_accounts`, `password_credentials` |
| Sessions | `sessions` (hashed tokens), `oauth_states`, `login_attempts` |
| Profiles | `profiles`, `skills`, `profile_skills` |
| Channels | `channels`, `channel_members` |
| Conversations | `direct_conversations`, `direct_conversation_members` |
| Messages | `messages` (channel, DM, thread replies, pins, soft deletes) |
| Engagement | `reactions`, `mentions`, `read_receipts`, `notifications` |
| Actions | `help_requests`, `decisions`, `tasks` |
| Cohort | `announcements` |
| Governance | `audit_events` |

Specifically persisted: account creation, OAuth links, password credentials,
sessions, profile updates, channel creation/rename/archive/restore, membership,
channel messages, direct messages, thread replies, reactions, mentions, read
receipts, pins, announcements, help requests and every transition, decisions
including superseding and reversal, tasks including assignment and status
changes, notifications, working statuses, and audit events.

## What is *not* used as storage

- React state — there is no React
- `localStorage` — **not used at all** in this build, for anything
- in-memory Python lists or dicts
- temporary files or the deployment filesystem

The rate limiter and the OAuth `state` store are the two things most likely to
be built in memory; both are database tables here, so they work correctly across
restarts and multiple instances.

## Transaction model

Service functions `flush()`, routes `commit()`. One request is one transaction.

```python
message = messages.create_message(db, author=user, channel=channel, body=body)
db.commit()
```

`create_message` writes the message, mention rows, notification rows, a read
receipt and an audit event. Either all of it lands or none of it does.

On failure:

1. the transaction rolls back
2. a structured error is returned (`{"error": {"code", "message", "retryable"}}`)
3. the UI shows it — an HTMX inline banner or a full error page
4. nothing partially-written remains

There is no code path that reports success without a committed row.

## Targeted mutations

Every write is scoped to what actually changed:

`create_channel` · `rename_channel` · `archive_channel` · `restore_channel` ·
`join_channel` · `leave_channel` · `create_message` · `edit_message` ·
`soft_delete_message` · `pin_message` · `unpin_message` · `add_reaction` ·
`remove_reaction` · `toggle_reaction` · `update_read_receipt` ·
`create_help_request` · `claim_help_request` · `unclaim_help_request` ·
`resolve_help_request` · `cancel_help_request` · `reopen_help_request` ·
`create_decision` · `supersede_decision` · `reverse_decision` · `create_task` ·
`assign_task` · `update_task_status` · `create_notification` ·
`mark_notification_read` · `mark_all_read` · `update_profile` ·
`set_working_status` · `set_user_role`

Nothing deletes and recreates a nested graph. Editing a message leaves its
reactions untouched (there is a test). Updating a profile's skills adds and
removes only the links that changed, preserving the rest.

## Soft deletion and retention

Messages are soft-deleted: `deleted_at` and `deleted_by_id` are set, the row and
its body remain, and the UI renders "This message was deleted." This keeps
threads coherent and the audit trail meaningful.

**Retention:** messages remain available indefinitely. Thirty days is the
minimum the brief requires; Ember has **no automatic deletion at all**. If a
retention policy is added later it must be documented here and implemented as an
explicit, audited job — not a silent background delete.

## Durability

Data survives page refresh, browser close, sign-out, sign-in, a different
browser, a different authorized device, a backend restart, and a redeploy —
because the only place it ever lived is PostgreSQL.

Verified by `app/tests/test_persistence.py`, which reads records back through a
**brand-new database session** (a separate connection and identity map) after
the writing session has committed, and by an HTTP test that writes, signs out,
and reads back from a second client with its own cookie jar.

## Verifying by hand

```bash
# after creating an account and posting a message in the UI
psql -d ember_dev -c "select count(*) from users;"
psql -d ember_dev -c "select body, created_at from messages order by seq desc limit 5;"
psql -d ember_dev -c "select action, entity_type, created_at
                      from audit_events order by created_at desc limit 10;"

# restart the backend entirely, reload the page — the data is still there
```

## Connection handling

`pool_pre_ping=True` validates a connection before use, which matters against
hosted PostgreSQL (Neon in particular) where idle connections are recycled.
`pool_size=10`, `max_overflow=20`, `pool_recycle=1800`. Use the **pooled** Neon
connection string in production.

## Backups

Ember does not implement its own backup mechanism. Use the provider's:

- **Neon** — point-in-time restore (retention depends on plan)
- **Render / Fly Postgres** — daily snapshots
- **Manual** — `pg_dump "$DATABASE_URL" > ember-$(date +%F).sql`
