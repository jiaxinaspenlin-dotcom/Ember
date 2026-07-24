# Near-real-time updates

Ember uses **polling**, not WebSockets or SSE. The brief prefers reliable polling
over fragile realtime, and at cohort scale polling is genuinely sufficient.

## The cursor

Every message gets `seq`, a bigint from the PostgreSQL sequence `message_seq`.
It is unique, monotonic and installation-wide, so "everything after N" is one
indexed comparison — no timestamp ties, no clock skew, no compound cursor.

## The endpoints

**JSON**

```
GET /api/messages/channel/{channel_id}/new?after_seq=412&limit=50
GET /api/direct-messages/{conversation_id}/messages/new?after_seq=87&limit=50

→ { "items": [...], "latest_seq": 415, "count": 3 }
```

**HTML fragments (what the UI actually uses)**

```
GET /hx/channels/{slug}/stream?after_seq=412
GET /hx/dm/{conversation_id}/stream?after_seq=87
```

Both run:

```sql
SELECT … FROM messages
WHERE channel_id = :id AND parent_message_id IS NULL AND seq > :after_seq
ORDER BY seq ASC LIMIT 50;
```

They return **only messages newer than the cursor**. They never return the whole
database, all channels, all conversations, or full history.

## How the page uses it

The channel page renders one poller element carrying the current cursor:

```html
<div id="message-poller"
     hx-get="/hx/channels/launch-week/stream?after_seq=412"
     hx-trigger="every 4000ms"
     hx-swap="outerHTML"
     hx-target="this"></div>
```

The response is the new message rows **plus a replacement poller** carrying the
new cursor. Because the element swaps itself, the cursor advances automatically
and the same message is never fetched twice.

Sending a message returns the new row plus an out-of-band poller update
(`hx-swap-oob`), so the sender's own message is not re-delivered by the next
poll.

The interval comes from `POLLING_INTERVAL_MS` (default 4000, within the 3–5 s
the brief allows) and is rendered into the template by Python.

## Cost

For 30 active members, all with a channel open:

- 30 ÷ 4 s ≈ **7.5 requests/second**
- each is one indexed query on `(channel_id, seq)`
- the overwhelming majority return **zero rows**
- response size when idle: a single `<div>`, well under 1 KB

The notification badge polls at 3× the interval (every 12 s) with a single
`COUNT` against `(recipient_id, read_at)`.

## What polling does *not* do

- ❌ reload all channels
- ❌ reload all conversations
- ❌ refetch full history each cycle
- ❌ send the whole database
- ❌ load thread replies (threads are opened deliberately and are not polled)

## Reading and unread counts while polling

When a poll returns rows and the viewer is a member, their read receipt advances
in the same request. Unread counts therefore stay correct without a separate
call, and they are correct on every device because they are derived from
`read_receipts` in SQL.

## Scrolling behaviour

`ember.js` scrolls to the bottom only if the user was already within 120 px of
it. Reading older messages is never interrupted by an incoming message.

## Loading older messages

Separate from polling, and explicit:

```
GET /hx/channels/{slug}/older?before_seq=380
```

Returns the previous 40 messages plus a new "Load older messages" button if more
exist. History is never loaded eagerly.

## Why not WebSockets or SSE

- They complicate deployment: sticky sessions, idle timeouts, proxy buffering,
  platform-specific connection limits.
- They need a reconnection-and-replay strategy, which is a cursor — the thing
  polling already is.
- At 30 users the latency difference is ≤ 4 seconds on a chat that is not
  latency-critical.

Both are reasonable future work if the deployment target supports them well; the
cursor endpoints would be reused as the replay mechanism. See
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## Tuning

| Members | Suggested `POLLING_INTERVAL_MS` |
| --- | --- |
| ≤ 30 | 4000 (default) |
| 30–75 | 5000 |
| > 75 | 6000–8000, and consider SSE |

## Tests

- `test_polling_returns_only_newer_messages`
- `test_polling_endpoint_does_not_replay_history`
- `test_polling_excludes_thread_replies_from_the_main_stream`
- `test_polling_fragment_returns_only_new_messages` (through the real HTMX route)
