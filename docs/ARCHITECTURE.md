# Architecture

## The frontend decision

The brief allowed two options: **FastAPI + Next.js** or
**FastAPI + Jinja2 + HTMX**. Ember uses the second.

### Why

**1. It makes the Python-first requirement structural rather than aspirational.**

The hard requirement is that permissions, unread counts, search results and
state transitions are never computed on the client. With a server-rendered UI
that is not a rule anyone has to remember — the button is not in the DOM unless
Python decided to render it, and the route re-checks anyway. With a separate
React app there is always a second data layer, and "just compute `canEdit` on
the client, it's obvious" is a one-line pull request away.

**2. Polling for new messages is a native idiom, not a synchronisation problem.**

```html
<div id="message-poller"
     hx-get="/hx/channels/launch-week/stream?after_seq=412"
     hx-trigger="every 4000ms"
     hx-swap="outerHTML"></div>
```

The server returns the new rows *and* a replacement poller carrying the new
cursor. There is no client-side cache to merge into, no deduplication, no
stale-closure bug. In a React app the same feature means a query cache, a
merge function, and a cursor kept in sync with it.

**3. One origin removes an entire category of deployment failure.**

The UI and the API are the same service, so the session cookie is a plain
same-origin `HttpOnly; SameSite=Lax` cookie. No CORS allow-list, no
`credentials: 'include'`, no third-party-cookie behaviour to worry about, no
bearer-token fallback. The brief explicitly warns about cross-domain cookie
unreliability; this design does not have the problem to begin with.

**4. Half the surface area means the whole product actually gets finished.**

The brief asks for ten feature areas *complete*, not scaffolded. Removing a
second application — its build, its types, its data layer, its deploy — is what
made finishing all ten realistic.

### What was given up, honestly

- **No optimistic UI.** Sending a message waits for the round trip (typically
  10–30 ms locally, well under 200 ms in production). Sends are not optimistic,
  so there is no rollback path to get wrong either.
- **Full page loads on navigation.** Pages are small and server-rendered;
  this is not perceptible at this scale.
- **No offline mode.** Out of scope for the brief.

### Where JavaScript *is* used

| File | Size | Purpose |
| --- | --- | --- |
| `htmx.min.js` | vendored | Fragment requests, polling |
| `alpine.min.js` | vendored | Mobile sidebar toggle, the action-menu tabs |
| `ember.js` | ~120 lines | Scroll management, Enter-to-send, error toasts |

All three are served from `/static`. There is no CDN dependency at runtime and
no build step in production.

## Layering

```
app/
├── main.py            app factory, error handlers, router registration
├── core/              config, enums, errors, logging, security primitives
├── db/                engine, session, declarative base
├── models/            SQLAlchemy 2 ORM (24 tables)
├── schemas/           Pydantic request/response models + serializers
├── auth/              passwords, sessions, github, permissions
├── services/          ALL business logic
├── search/            permission-aware full-text search
├── api/routes/        thin JSON endpoints  ─┐
├── web/routes/        thin HTML endpoints  ─┴─ both call services/
├── templates/         Jinja2
└── static/            css, js, fonts, logo
```

**The rule:** routes parse input, call one service function, commit, and render.
They never contain business logic. Both route layers call the *same* service
functions, which is why the JSON API and the UI can never disagree about what is
allowed.

### Why `services/` and not fat models

Business operations here span several tables — creating a message also parses
mentions, writes notification rows, advances a read receipt and appends an audit
event. That is a unit-of-work concern, not a single-entity concern. Service
functions take an explicit `Session`, so the caller controls the transaction
boundary and tests can drive them directly without HTTP.

## Transaction model

Service functions `flush()`; routes `commit()`. One request is one transaction.

```python
message = messages.create_message(db, author=user, channel=channel, body=body)
db.commit()          # message + mentions + notifications + receipt + audit
```

If any step raises, nothing is committed, the error handler returns a structured
error, and the UI shows it. There is no partial write and no fake success.

## Error handling

Every service failure is an `EmberError` subclass carrying a code, a status and
a `retryable` flag. `main.py` registers handlers that render:

- **JSON** for `/api/*` — `{"error": {"code", "message", "retryable"}}`
- **an inline banner** for HTMX requests
- **a full error page** for ordinary page loads

`SQLAlchemyError` and unhandled exceptions are caught too, logged by type only,
and returned as retryable 503/500 responses. Errors are never converted into
empty states.

## Performance decisions

| Concern | Approach |
| --- | --- |
| Message history | Cursor pagination on an indexed `seq` column, 40 per page |
| Polling | `WHERE seq > :cursor` — usually returns zero rows |
| Threads | `reply_count` denormalised on the parent; replies fetched only when a thread is opened |
| Reactions | `selectinload` on the message page — one extra query, not N |
| Unread counts | Single SQL aggregate per scope, joined against read receipts |
| Dashboard | Nine bounded queries, each `LIMIT 5` or a `COUNT` |
| Search | GIN index on `tsvector`, `LIMIT`-capped at 100 |
| Sidebar | 50 channels + 20 conversations, unread counts computed in the same query |

No endpoint loads the full table for an ordinary page.
