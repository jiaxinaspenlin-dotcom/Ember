# Help Queue

A cohort-wide queue so nobody stays blocked in silence.

## The state machine

```
                ┌──────────── reopen ────────────┐
                │                                │
                ▼                                │
   ┌────────► OPEN ──── claim ────► CLAIMED ─────┤
   │           │  ▲                    │         │
   │           │  └──── unclaim ───────┘         │
   │           │                                 │
   │           ├──── resolve ──────► RESOLVED ───┤
   │           │                                 │
   │           └──── cancel ───────► CANCELLED ──┘
   │                                             │
   └───────────────── reopen ────────────────────┘
```

Implemented in `app/services/help_requests.py`:

```python
ALLOWED_TRANSITIONS = {
    OPEN:      {CLAIMED, RESOLVED, CANCELLED},
    CLAIMED:   {OPEN, RESOLVED, CANCELLED},
    RESOLVED:  {OPEN},
    CANCELLED: {OPEN},
}
```

Two independent gates apply to every transition:

1. **Is the move legal?** → `can_transition()`, else `409
   HELP_REQUEST_INVALID_TRANSITION`
2. **Is this person allowed to make it?** → `permissions.py`, else `403`

Database CHECK constraints back the invariants: `claimed` requires a helper and
a `claimed_at`; `resolved` requires `resolved_at`; `open` must have no helper.

## Who may do what

| Action | Requester | Helper | Admin | Other member |
| --- | --- | --- | --- | --- |
| Create | ✅ | ✅ | ✅ | ✅ |
| Claim | ❌ (own) | — | ✅ | ✅ |
| Unclaim | ❌ | ✅ | ✅ | ❌ |
| Resolve | ✅ | ✅ | ✅ | ❌ |
| Cancel | ✅ | ❌ | ✅ | ❌ |
| Reopen | ✅ | ❌ | ✅ | ❌ |
| Edit | ✅ | ❌ | ✅ | ❌ |

You cannot claim your own request — claiming means "I will help with this."

## Fields

`id` · `title` · `description` · `original_message_id` · `requester_id` ·
`source_channel_id` · `category` · `urgency` · `status` · `assigned_helper_id` ·
`created_at` · `claimed_at` · `resolved_at` · `cancelled_at` ·
`resolution_note`

**Categories:** Coding · Design · Deployment · Research · Product · Feedback ·
Other
**Urgency:** Low · Normal · High · Urgent

## Creating one

Two routes, one service function:

1. **From a message** — "Turn into… → Help request" on any public channel
   message. The form is pre-filled from the message body, and
   `original_message_id` / `source_channel_id` are kept so the queue links back
   to the conversation. Direct messages cannot be converted.
2. **Directly** — the "Ask for help" form at the top of `/help`.

**Feedback requests** are help requests with `category = feedback`. They share
the model, the queue and the state machine, exactly as the brief allows.

## The queue

`/help` shows open, claimed and recently resolved requests with urgency,
category, requester, assigned helper, time open and a link to the source
message. Counts for open/claimed/resolved appear as tiles.

**Filters:** status · category · urgency · assigned to me · created by me ·
unclaimed · free-text search over title and description. All are applied in SQL
with `LIMIT`/`OFFSET` pagination (20 per page).

## Notifications

| Transition | Who is notified |
| --- | --- |
| Claimed | the requester |
| Resolved | the requester and the helper (excluding the actor) |
| Reopened | the requester |
| Unclaimed by someone else | the previous helper |
| Cancelled while claimed | the previous helper |

Nobody is ever notified about their own action.

## Resolution notes

Resolving accepts an optional note. It is worth writing: the note is stored on
the request, shown on the detail page, and searchable — which is how a question
answered in week two is still findable in week eight.

## Reopening

Reopening clears `resolved_at`, `cancelled_at`, `assigned_helper_id` and
`claimed_at`, returning the request to the queue cleanly. The history is not
lost: every transition is in `audit_events`.

## Tests

`app/tests/test_actions.py` covers creation from a message, the full
claim → resolve path, self-claim rejection, unclaim, admin resolution, reopen,
cancel-then-reopen, illegal transitions, the parameterised transition table, and
every queue filter. `test_web_flows.py` walks the same journey through the UI.
