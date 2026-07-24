# Decision Log

The answer to "why did we do it this way?" six weeks later.

## The state machine

```
                ┌── supersede (+ replacement) ──► SUPERSEDED  (terminal)
   ACTIVE ──────┤
                └── reverse (+ reason) ─────────► REVERSED    (terminal)
```

```python
ALLOWED_TRANSITIONS = {
    ACTIVE:     {SUPERSEDED, REVERSED},
    SUPERSEDED: frozenset(),
    REVERSED:   frozenset(),
}
```

Both end states are terminal **by design**. A decision that was superseded stays
superseded; what changed is captured by the *replacement* decision, not by
mutating history. Nothing is ever deleted.

## Who may do what

| Action | Author | Admin | Other member |
| --- | --- | --- | --- |
| Create | ✅ | ✅ | ✅ |
| Read / search | ✅ | ✅ | ✅ |
| Edit (while active) | ✅ | ✅ | ❌ |
| Supersede | ✅ | ✅ | ❌ |
| Reverse | ✅ | ✅ | ❌ |
| Delete | ❌ | ❌ | ❌ |

## Fields

`id` · `title` · `decision_text` · `context` · `original_message_id` ·
`source_channel_id` · `author_id` · `related_project` · `status` ·
`superseded_by_id` · `superseded_at` · `reversed_at` · `reversed_by_id` ·
`reversal_reason` · timestamps

- **`decision_text`** — what was decided
- **`context`** — why: alternatives considered, constraints, who was involved
- **`related_project`** — free-text grouping, also used as a filter

## Creating one

1. **From a message** — "Turn into… → Decision" on any public channel message.
   `original_message_id` and `source_channel_id` are preserved, so the log links
   straight back to the discussion that produced the decision.
2. **Directly** — the "Record a decision" form on `/decisions`.

## Superseding

```
POST /decisions/{id}/supersede    { superseded_by_id }
```

Rules, all enforced in Python:

- the decision being superseded must be **active**
- the replacement must exist, be **active**, and not be the same decision
- only the author or an admin may do it

The result sets `status = superseded`, `superseded_by_id` and `superseded_at`.
The detail page then shows a link to the replacement, and the replacement stays
active.

A database CHECK enforces the pairing: `superseded` ⇔ `superseded_by_id IS NOT
NULL`. The two cannot drift apart.

If no replacement exists yet, record it first, then link it — the UI says so
rather than offering an empty dropdown.

## Reversing

```
POST /decisions/{id}/reverse    { reason }
```

For a decision that was undone without anything replacing it. Stores
`reversed_at`, `reversed_by_id` and `reversal_reason`, all of which are shown on
the detail page.

## Searching and filtering

`decisions.search_vector` is a `GENERATED ALWAYS AS STORED` tsvector over title,
decision text and context, with a GIN index. `/decisions` supports:

- full-text search (`websearch_to_tsquery`, with a `LIKE` fallback on title)
- status: active / superseded / reversed
- source channel
- author
- related project

Decisions are also included in global search (`/search?scope=decisions`).

Every result links to the original message when there is one.

## Notifications

If someone *other than the author* supersedes or reverses a decision, the author
is notified (`decision_changed`). Self-actions never notify.

## Why terminal states

An alternative design would let a superseded decision return to active. That
loses the property that makes a decision log worth keeping: at any point in
history, exactly one decision in a chain is current, and the chain reads in one
direction. "Un-superseding" would make the log a mutable document rather than a
record. If a reversal was wrong, record a new decision saying so — which is also
what a team does in real life.

## Tests

`app/tests/test_actions.py` covers creation from a message, superseding with a
replacement, self-supersede rejection, double-supersede rejection, reversal with
a reason, author-or-admin enforcement, author notification, every filter, and
the parameterised transition table. `test_web_flows.py` walks superseding
through the UI.
