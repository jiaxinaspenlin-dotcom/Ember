# Security posture

What Ember defends against, how, and what remains open. Written after a
line-by-line audit of the codebase; the findings that audit produced were fixed
and are covered by `app/tests/test_hardening.py`.

## Response headers

Every response carries:

| Header | Value | Stops |
| --- | --- | --- |
| `Content-Security-Policy` | see below | External script injection, framing, `<base>` hijacking, plugins, off-site form posts |
| `X-Frame-Options` | `DENY` | Clickjacking (older browsers) |
| `X-Content-Type-Options` | `nosniff` | MIME confusion |
| `Referrer-Policy` | `strict-origin-when-cross-origin` | Leaking paths (e.g. `/dm/{id}`) to third parties |
| `Permissions-Policy` | camera/mic/geolocation off | Unwanted capability access |
| `Strict-Transport-Security` | 1 year, production only | Protocol downgrade |

```
default-src 'self';
script-src 'self' 'unsafe-inline' 'unsafe-eval';
style-src 'self' 'unsafe-inline';
img-src 'self' https: data:;
font-src 'self'; connect-src 'self'; form-action 'self';
frame-ancestors 'none'; base-uri 'self'; object-src 'none'
```

**Honest caveat:** `unsafe-inline` and `unsafe-eval` are required by the chosen
stack — Alpine.js evaluates its directives through the `Function` constructor,
and a few HTMX/Alpine handlers plus the avatar tint are inline attributes. They
weaken the anti-XSS value of the policy. The clauses that *cannot* be relaxed
away still carry real weight: no external script origin can load, nothing can
frame the app, `<base>` cannot be rewritten, plugins are dead, and forms can
only post back to Ember. Removing `unsafe-eval` would mean switching to Alpine's
CSP build and replacing the remaining inline handlers.

## Cross-site request forgery

- Session cookie is `HttpOnly; SameSite=Lax; Secure` (production).
- Every state change is `POST`/`PATCH`/`PUT`/`DELETE`; no `GET` mutates.
- `form-action 'self'` and no CORS middleware at all.
- OAuth uses a single-use `state` row stored in PostgreSQL.

`SameSite=Lax` means a cross-site form post does not carry the cookie, which is
what a synchroniser token would otherwise buy. If Ember is ever split across
origins, add one — see [KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## Cross-site scripting

Jinja2 autoescaping is on for every template. The one place that produces raw
markup is `render_body()`, which **escapes first** and only then linkifies URLs
and highlights mentions:

```python
safe = escape(body or "")          # &, <, >, ", ' are gone before anything else
safe = _URL_RENDER.sub(...)        # only matches https?:// (never javascript:)
safe = _MENTION_RENDER.sub(...)    # capture group is [A-Za-z0-9._-] only
```

Because escaping happens first, a `"` in a crafted URL is already `&quot;` and
cannot terminate the `href` attribute. `javascript:` URLs never match the
pattern. A test posts `<script>alert('xss')</script>` and asserts the rendered
page contains `&lt;script&gt;` and not the live tag.

Avatar URLs are validated to start with `http://` or `https://` and are rendered
through autoescaping.

## Authentication and sessions

Covered in detail in [AUTHENTICATION.md](AUTHENTICATION.md). Summary:

- Argon2 password hashing; constant-time verification, including a dummy-hash
  path so a missing account takes the same time as a wrong password
- Opaque 32-byte session tokens; **only the SHA-256 hash is stored**
- Revocation checked on every request; a password change *or a completed reset*
  revokes all sessions
- Roles are only settable server-side (config at creation, CLI, or an
  authenticated admin) — never from a payload, URL or browser state

### Email verification and password reset

Both flows use `email_tokens`, where **only the token hash is stored**, and
each token is single-use, expiring and bound to the address it was sent to (see
[EMAIL.md](EMAIL.md)). Neither flow discloses whether an address is registered:

- **Password reset** (`/api/auth/password/forgot`) responds identically for a
  known address, an unknown address, and a GitHub-only account. Completing a
  reset revokes every existing session for that user.
- **Email verification** (opt-in, `REQUIRE_EMAIL_VERIFICATION=true`) makes signup
  return an identical response whether or not the address already exists — the
  real owner of a re-used address is emailed instead. Unverified accounts are
  gated out of everything but the auth endpoints.

## Rate limiting

Independent budgets, never combined:

| Budget | Default | Purpose |
| --- | --- | --- |
| Login, per account (email) | 8 / 15 min | Brute force against one person |
| Login, per source address | 40 / 15 min | Credential spraying across accounts |
| Signup, per source address | 10 / 15 min | Bulk account creation, enumeration at scale |
| Password reset, per source address | 10 / 15 min | Reset-email flooding |
| Verification resend, per source address | 10 / 15 min | Confirmation-email flooding |

> **Why they are separate.** The first implementation OR-ed both into a single
> count against one threshold. Behind a reverse proxy — where every member
> shares one source address unless `X-Forwarded-For` is trusted — eight failed
> logins from *anyone* would have locked out the *entire cohort* for fifteen
> minutes. That was an availability flaw, and `test_per_account_and_per_ip_budgets_are_independent`
> now pins the correct behaviour.

Attempts are rows in PostgreSQL, so the limit survives restarts and works across
instances. `ember-admin purge-sessions` trims rows older than a day.

## Proxy headers

`X-Forwarded-For` is attacker-controlled unless a trusted proxy sets it, and it
feeds the rate limiter — so it is honoured **only** when `TRUST_PROXY_HEADERS`
is true. `render.yaml` and `fly.toml` set it; the default is `false` so a
directly-exposed deployment cannot be spoofed.

## Production configuration guard

The application **refuses to boot** with `ENVIRONMENT=production` if:

- `SESSION_SECRET` is still the development placeholder
- `SESSION_SECRET` is shorter than 32 characters
- `BACKEND_URL` or `FRONTEND_URL` is not `https://`

A misconfigured deploy fails loudly at startup rather than quietly serving
insecure cookies.

## Denial of service

- Password and form fields are length-capped **before** reaching Argon2, so an
  oversized body cannot become a CPU sink.
- Every list endpoint is paginated; search results are capped at 100.
- Message bodies are capped at 8,000 characters.
- Polling queries are single indexed comparisons that usually return nothing.

## Privacy

Full matrix in [PERMISSIONS_AND_PRIVACY.md](PERMISSIONS_AND_PRIVACY.md).
The load-bearing points:

- **Administrators have no access to direct messages.** Non-participants get
  `404`, not `403` — the existence of a conversation is itself private.
- Search filters permissions **inside the SQL statement**, not afterwards.
- `UserSummary` — the shape used for every other member — has **no email
  field at all**. Emails appear only in your own `CurrentUserOut`.
- Notifications are filtered by `recipient_id` in SQL; acting on someone
  else's returns 404.
- Logs and audit context are scrubbed of passwords, tokens, cookies and message
  bodies; database errors log the exception *type* only.

## Accepted risks

| Risk | Why it is accepted | How to close it |
| --- | --- | --- |
| **Signup reveals whether an email is registered** *(only when verification is off — the default)* | With `REQUIRE_EMAIL_VERIFICATION=true` this is fully closed: signup responds identically either way. With it off, the immediate-usability convenience is kept and rate limiting bounds enumeration. | Set `REQUIRE_EMAIL_VERIFICATION=true` (requires SMTP) |
| **`unsafe-inline` / `unsafe-eval` in CSP** | Required by Alpine.js and inline HTMX handlers | Alpine CSP build + move handlers into `ember.js` |
| **No CSRF synchroniser token** | `SameSite=Lax` + same-origin covers it today | Required if the app is ever split across origins |
| **Avatar URLs load from third-party hosts** | GitHub avatars are the point | Proxy or self-host images |
| **Non-members can react to public channel messages** | Channels are readable cohort-wide, so this leaks nothing — reacting simply is not gated on membership the way posting is | Add a membership check in `add_reaction` |
| **No global request rate limit** | Trusted-cohort deployment | Add a limiter at the proxy or middleware |
| **Sessions have absolute (30-day) expiry, no idle timeout** | Matches the brief | Add `last_seen_at` based idle expiry |

## What was found and fixed in the audit

1. **Rate-limit budgets OR-ed together** → cohort-wide lockout behind a proxy.
   Split into independent per-account and per-IP budgets.
2. **No security response headers** → clickjacking on destructive controls.
   Added CSP, `X-Frame-Options`, `nosniff`, `Referrer-Policy`,
   `Permissions-Policy`, HSTS.
3. **`SESSION_SECRET` had a usable insecure default** → a production deploy
   could silently run on it. Now refuses to boot.
4. **`X-Forwarded-For` trusted unconditionally** → spoofable rate-limit key.
   Now gated behind `TRUST_PROXY_HEADERS`.
5. **Unbounded password length on the HTML forms** → Argon2 CPU sink.
   Capped at 200 characters.
6. **Signup was not rate limited** → free bulk creation and enumeration oracle.
   Now limited per source address, and failed attempts count.
7. **Self-demotion blocked in the web route but not the JSON API** → an admin
   could strand an installation with no administrator. The guard now lives in
   the service, so both paths share it; the CLI keeps the operator escape hatch.

Each has a regression test in `app/tests/test_hardening.py`.

## Later additions

- **Password reset and email verification** were added with the non-disclosure,
  hash-at-rest, single-use, session-revoking properties described above.
  Covered by `app/tests/test_email_flows.py` (20 tests).
- **GitHub OAuth** gained end-to-end coverage of the live handshake — authorize
  redirect, DB-stored single-use `state`, code exchange, identity fetch, account
  linking and token handling — against a mocked HTTP transport, in
  `app/tests/test_github_oauth_flow.py` (7 tests). The raw calls to `github.com`
  are the only thing stubbed.
