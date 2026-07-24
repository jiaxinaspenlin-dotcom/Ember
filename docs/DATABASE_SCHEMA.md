# Database schema

PostgreSQL 14+. 25 application tables plus `alembic_version`. Everything is
normalised; JSONB is used only for small optional metadata (`audit_events.context`,
`messages.extra`).

Ember is **multi-tenant**: a **cohort** is a workspace, and every tenant-owned
table carries a non-null `cohort_id`. Identity is global (`users`); role and
profile are per-cohort (`cohort_memberships`). See [COHORTS.md](COHORTS.md).

## Identity and access

### `users`
`id` uuid PK · `email` varchar(320) **unique, nullable** · `email_verified` bool ·
`display_name` varchar(120) · `avatar_url` · `is_active` bool · `last_login_at` ·
timestamps

Identity only — there is **no** global `role` or `profile` here; those live on
`cohort_memberships`. `email` is nullable because GitHub may not expose one; when
present it is stored normalised and is unique installation-wide.
*Index:* `created_at`.

### `oauth_accounts`
`id` · `user_id` → users CASCADE · `provider` · `provider_account_id` ·
`provider_username` · `provider_email` · `scopes` · `access_token_encrypted`
*Unique:* `(provider, provider_account_id)` — the stable linking key.
The token column is Fernet-encrypted, never serialised, never logged.

### `password_credentials`
`id` · `user_id` → users CASCADE **unique** · `password_hash` (Argon2)

### `sessions`
`id` · `user_id` → users CASCADE · `token_hash` **unique** · `created_at` ·
`last_seen_at` · `expires_at` · `revoked_at` · `user_agent` · `ip_address`
*Index:* `(user_id, expires_at)`. Only the hash is stored.

### `oauth_states`
`id` · `state` **unique** · `provider` · `redirect_to` · `expires_at` ·
`consumed_at` — server-side CSRF state, so validation never depends on process
memory.

### `login_attempts`
`id` · `identifier` · `ip_address` · `successful` · `created_at`
*Indexes:* `(identifier, created_at)`, `(ip_address, created_at)` — backs the
rate limiter, which therefore survives restarts and multiple instances. Reserved
identifiers (`[signup]`, `[password-reset]`, `[email-verify]`) throttle those
endpoints in the same table.

### `email_tokens`
`id` · `user_id` → users CASCADE · `purpose` (`verify_email`|`reset_password`) ·
`token_hash` **unique** · `email` · `created_at` · `expires_at` · `consumed_at` ·
`requested_ip`
*Indexes:* `(user_id, purpose)`, `expires_at`.

Backs password reset and email verification. Only the **hash** of the token is
stored, so a database read cannot be replayed as a working link. Each token is
single-use (`consumed_at`), expiring, and bound to the `email` it was issued for
— changing the address invalidates any outstanding token.

## Cohorts and membership

### `cohorts`
`id` uuid PK · `slug` varchar(60) **unique** · `name` varchar(80) ·
`description` · `invite_code` varchar(64) **unique, nullable** ·
`created_by_id` → users RESTRICT **nullable** (null for system/CLI-seeded
cohorts) · timestamps

A cohort is a workspace. `invite_code`, when set, is the opaque join code behind
`/join/{invite_code}`.

### `cohort_memberships`
`id` uuid PK · `cohort_id` → cohorts CASCADE · `user_id` → users CASCADE ·
`role` (`member`|`admin`) · `joined_at` · **per-cohort profile:** `bio` ·
`current_project` · `project_area` · `working_status` · `available_to_help` bool ·
`profile_completed` bool · timestamps
*Unique:* `(cohort_id, user_id)` — one membership per person per cohort.
*Indexes:* `(cohort_id, available_to_help)`, `(cohort_id, working_status)`.

This is the join between a `User` and a `Cohort`, and it carries the role and the
profile that used to live on `users`/`profiles`. Leaving a cohort deletes the row.

### `skills` / `membership_skills`
`skills`: `id` · `slug` **unique** · `name` — a global registry, created on
demand by members; none are seeded.
`membership_skills`: composite PK `(membership_id, skill_id)` · `position`
(CHECK ≥ 0). Skills are attached to a **membership**, so the same person can list
different skills in different cohorts.

## Channels and conversations

### `channels`
`id` · `cohort_id` → cohorts CASCADE · `slug` · `name` · `description` · `topic` ·
`invite_code` **unique, nullable** · `is_archived` · `archived_at` ·
`archived_by_id` · `created_by_id` (RESTRICT) · timestamps
*Unique:* `(cohort_id, slug)` — slugs are unique **within a cohort**, so two
cohorts can each have a `#general`. *Index:* `is_archived`. Renaming never
changes the slug, so links keep working. `created_by` is the channel's admin;
`invite_code` is the opaque shareable join code (null when no link is active) and
is never exposed in channel payloads.

> Every other tenant-owned table — `direct_conversations`, `messages`,
> `help_requests`, `decisions`, `tasks`, `announcements`, `notifications` —
> likewise carries a non-null `cohort_id` (CASCADE), and every read is filtered
> by it. `sessions` carries `active_cohort_id` (→ cohorts, SET NULL) to remember
> the workspace in use.

### `channel_members`
`id` · `channel_id` · `user_id` · `joined_at`
*Unique:* `(channel_id, user_id)`. *Index:* `user_id`.

### `direct_conversations`
`id` · `pair_key` **unique** · `created_by_id` · `created_at` · `last_message_at`

`pair_key` is the canonical `min(uuid):max(uuid)` string, so a pair can only
ever have one conversation — enforced by the database, not by a race-prone
"check then insert".

### `direct_conversation_members`
`id` · `conversation_id` · `user_id` · `joined_at`
*Unique:* `(conversation_id, user_id)`. *Index:* `user_id`.

## Messages

### `messages`
`id` uuid PK · `seq` bigint **unique** (from sequence `message_seq`) ·
`sender_id` (RESTRICT) · `channel_id` nullable · `direct_conversation_id`
nullable · `parent_message_id` nullable (self-FK) · `body` text ·
`message_type` · `created_at` · `edited_at` · `deleted_at` · `deleted_by_id` ·
`is_pinned` · `pinned_at` · `pinned_by_id` · `reply_count` · `last_reply_at` ·
`extra` jsonb · `search_vector` tsvector GENERATED STORED

*CHECK `exactly_one_destination`:*
```sql
(channel_id IS NOT NULL AND direct_conversation_id IS NULL)
OR (channel_id IS NULL AND direct_conversation_id IS NOT NULL)
```

*Indexes:* `(channel_id, created_at)`, `(channel_id, id)`,
`(direct_conversation_id, created_at)`, `parent_message_id`,
`(sender_id, created_at)`, `is_pinned`, `seq`, GIN on `search_vector`.

Deletion is soft (`deleted_at`), so threads and the audit trail stay intact.

### `reactions`
`id` · `message_id` CASCADE · `user_id` CASCADE · `reaction_type` · `created_at`
*Unique:* `(message_id, user_id, reaction_type)` — the same user cannot add the
same reaction twice, guaranteed by the database.

### `mentions`
`id` · `message_id` CASCADE · `mention_type` (`user`|`channel`|`admins`) ·
`mentioned_user_id` · `raw_text` · `created_at`
*CHECK:* a `user` mention must carry a `mentioned_user_id`.

### `read_receipts`
`id` · `user_id` · `channel_id` nullable · `direct_conversation_id` nullable ·
`last_read_message_id` · `last_read_seq` bigint · `last_read_at`
*CHECK:* exactly one scope. *Unique:* `(user_id, channel_id)` and
`(user_id, direct_conversation_id)`.

## Actions

### `help_requests`
`id` · `title` · `description` · `original_message_id` · `requester_id` ·
`source_channel_id` · `category` · `urgency` · `status` ·
`assigned_helper_id` · `claimed_at` · `resolved_at` · `cancelled_at` ·
`resolution_note` · timestamps

*CHECKs:* `claimed` requires a helper and a `claimed_at`; `resolved` requires
`resolved_at`; `open` must have no helper.
*Indexes:* `status`, `assigned_helper_id`, `requester_id`, `(status, created_at)`.

### `decisions`
`id` · `title` · `decision_text` · `context` · `original_message_id` ·
`source_channel_id` · `author_id` · `related_project` · `status` ·
`superseded_by_id` (self-FK) · `superseded_at` · `reversed_at` ·
`reversed_by_id` · `reversal_reason` · `search_vector` GENERATED · timestamps

*CHECKs:* `superseded` ⇔ `superseded_by_id IS NOT NULL`; `reversed` requires
`reversed_at`.
*Indexes:* `status`, `author_id`, `source_channel_id`, GIN on `search_vector`.

### `tasks`
`id` · `title` · `description` · `creator_id` (RESTRICT) · `assignee_id`
(SET NULL) · `source_message_id` · `source_channel_id` · `status` ·
`priority` · `due_at` · `completed_at` · timestamps

*CHECK:* `done` requires `completed_at`.
*Indexes:* `assignee_id`, `status`, `(assignee_id, status)`, `creator_id`.

## Engagement

### `notifications`
`id` · `recipient_id` CASCADE · `actor_id` · `notification_type` · `title` ·
`body` · `link_path` · nullable FKs to message/channel/conversation/help
request/decision/task/announcement · `read_at` · `created_at`
*Indexes:* `(recipient_id, read_at)`, `(recipient_id, created_at)`.

### `announcements`
`id` · `title` · `body` · `author_id` (RESTRICT) · `priority` · `published_at` ·
`expires_at` · `is_pinned` · timestamps
*Indexes:* `published_at`, `is_pinned`.

### `audit_events`
`id` · `actor_id` (SET NULL) · `action` · `entity_type` · `entity_id` ·
`context` jsonb (scrubbed) · `ip_address` · `created_at`
*Indexes:* `(actor_id, created_at)`, `(action, created_at)`,
`(entity_type, entity_id)`.

## Enum storage

Enums are stored as `VARCHAR` with a `CHECK` constraint listing the permitted
values (SQLAlchemy `Enum(..., native_enum=False)`). This keeps every value valid
at the database level while avoiding the migration pain of native PostgreSQL
enum types.

## Verifying

```bash
alembic upgrade head
alembic check          # "No new upgrade operations detected."
psql -d ember_dev -c "\d messages"
psql -d ember_dev -c "\di"
```
