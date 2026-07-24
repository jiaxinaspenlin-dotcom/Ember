# Known limitations

An honest list of what Ember does not do, and why. Nothing here is a
half-finished feature or a placeholder button — features that are not built are
simply not present in the UI.

## Not implemented, deliberately

### ~~Password reset by email~~ — implemented
A full forgotten-password flow now ships: neutral responses, single-use
hash-at-rest tokens, session revocation on completion, and a per-IP rate limit.
See [EMAIL.md](EMAIL.md). Works over SMTP, or the console backend in
development.

### ~~Email verification~~ — implemented (opt-in)
`REQUIRE_EMAIL_VERIFICATION=true` gates new email/password accounts until they
confirm, and makes signup stop disclosing whether an address is registered. Off
by default so the app is usable with zero email configuration; see
[EMAIL.md](EMAIL.md).

### Deliverability is your responsibility
Ember sends correct mail, but landing in the inbox needs SPF/DKIM/DMARC on the
sending domain — configure these with your transactional provider.

### File and image uploads
Messages are text. No attachments, no avatars uploaded to Ember — avatars are
URLs (from GitHub, or entered manually). Adding uploads means object storage
(S3/R2) and a virus-scanning decision; the deployment filesystem must never be
used, so this was out of scope rather than done badly.

### Group direct messages
Direct conversations are strictly one-to-one, enforced by the `pair_key` unique
constraint. Group DMs would need a different uniqueness model. Channels cover
group conversation.

### Private or invite-only channels
All channels are public to the cohort; archived channels stay readable. The
permission layer is structured so a `visibility` column and a membership check in
`can_view_channel()` would be a contained change.

### Message search inside a thread only
Search covers all messages including replies, but there is no "search within this
thread" scope.

### Rich text and markdown
Message bodies are plain text. URLs are linkified and mentions highlighted, both
**after** HTML escaping. Markdown would need a sanitising renderer; escaping-first
was the safer default for this build.

### Typing indicators, presence, read-by lists
Working status is manual (Available to help / Building / In focus mode /
Blocked / Away). There is no automatic presence, no typing indicator, and read
receipts drive unread counts but are not exposed as "seen by" lists.

### WebSockets / Server-Sent Events
Polling every 4 seconds, by design — see [POLLING.md](POLLING.md). Both are
reasonable future work; the existing cursor endpoints would become the replay
mechanism.

### Push, email or digest notifications
Notifications are in-app only, and persisted. There is no email digest and no
web-push.

### Data export / GDPR tooling
No self-serve export or account deletion. `pg_dump` and SQL are the current
answer. Deleting a user would need a decision about their messages — soft-delete
authorship or cascade — that is a product decision, not a technical one.

### Internationalisation
English only. Timestamps render in UTC.

## Behaviours worth knowing

| Behaviour | Detail |
| --- | --- |
| **Message edit window** | 24 hours, author only. Admins can delete but never rewrite. |
| **Retention** | Messages are kept indefinitely. Nothing is auto-deleted; 30 days is a floor, not a target. |
| **Soft deletes** | The row and body remain for audit; the UI shows "This message was deleted." |
| **Terminal decision states** | Superseded and reversed are final — record a new decision instead. |
| **Self-claim** | You cannot claim your own help request. |
| **Admins and DMs** | Administrators have **no** access to conversations they are not in. |
| **Channel admins** | Any member can create a channel and becomes its admin — inviting/removing members, sharing an invite link, renaming, archiving and pinning. This is scoped to their channels and is **not** the installation-wide admin role. Installation admins manage any channel. |
| **Channels are public** | Invite links and direct invites are a convenience; anyone in the cohort can still browse and join any channel. Private/invite-only channels are not built. |
| **Timezones** | Everything is stored and displayed in UTC. |
| **Mention matching** | Display names are matched case-insensitively, ignoring spaces, hyphens and underscores. Two members with names that normalise identically would both match; display names are not unique. |
| **Skills** | Free text, deduplicated by slug. No controlled vocabulary. |

## Operational limitations

- **Upgrading a pre-tenancy database is a one-time bridge script, not an
  `alembic upgrade`.** The multi-tenant schema shipped as a fresh Alembic
  baseline, so a database from an earlier single-tenant build cannot upgrade onto
  it in the normal chain. Instead, run the **data-preserving bridge** (back up
  first):
  ```bash
  python -m app.db.legacy_migration      # against the legacy DATABASE_URL
  ```
  It moves the old tables into a `legacy` Postgres schema, builds the canonical
  multi-tenant schema, then copies every row across: a single **default cohort**
  is created, each user's global `role` + `profiles` row becomes their
  `CohortMembership`, skills are re-homed, every tenant row is stamped with the
  default `cohort_id`, the message sequence is preserved, and Alembic is stamped
  at head. It refuses to run unless the database is actually on the old schema.
  The transform is verified end-to-end against a seeded legacy database in
  [`app/tests/test_legacy_migration.py`](../backend/app/tests/test_legacy_migration.py).
  **Migrations from the multi-tenant baseline forward are ordinary Alembic
  revisions** — this only concerns the one-time jump *onto* multi-tenancy.
- **Cohort creation is capped per account** (`MAX_COHORTS_CREATED_PER_USER`,
  default 10; `0` disables). This bounds the open-join spam surface; it does not
  limit how many cohorts a person can *join*, or messages/channels within one.
- **Boot waits for the database** up to `DB_CONNECT_MAX_ATTEMPTS` with linear
  backoff, so an app that starts a beat before Postgres is ready recovers instead
  of crash-looping. A database that is genuinely down still fails the boot loudly.
- **Scale target ~30 active members.** Beyond ~75, raise the poll interval and
  consider SSE.
- **Single instance assumed.** Nothing prevents horizontal scaling — all state is
  in PostgreSQL — but it has not been load-tested multi-instance.
- **Neon cold starts.** Free-tier compute suspends when idle; the first request
  after idle is slow. `pool_pre_ping` handles the reconnect.
- **Rate limiting covers sign-in and signup only.** Message posting, search and
  polling are unthrottled. Fine for a trusted cohort; add a limiter at the proxy
  for a public deployment.
- **Signup reveals whether an email is registered — only when verification is
  off** (the default). Turning on `REQUIRE_EMAIL_VERIFICATION` closes it
  entirely. See [SECURITY.md](SECURITY.md#accepted-risks).
- **No CSRF synchroniser token.** Protection relies on `SameSite=Lax` cookies,
  same-origin deployment, `form-action 'self'` and state-changing verbs, which is
  sound for a same-origin app. Splitting origins would require adding one.
- **The CSP allows `unsafe-inline` and `unsafe-eval`**, because Alpine.js
  evaluates directives via the `Function` constructor. The policy still blocks
  external script origins, framing, plugins and off-site form posts. See
  [SECURITY.md](SECURITY.md).
- **Audit log has no UI beyond the last 25 events** on `/admin`. Query
  `audit_events` directly for more.

## Accessibility

Implemented: semantic HTML, one `<h1>` per page, labelled controls, ARIA labels
on icon-only buttons, visible focus rings, keyboard-navigable menus and dialogs,
skip-to-content link, `aria-live` toasts, `aria-pressed` on reaction toggles,
`prefers-reduced-motion` support, and contrast-checked colours.

Not done: a full screen-reader audit with JAWS/NVDA/VoiceOver, and automated
axe-core testing in CI.
