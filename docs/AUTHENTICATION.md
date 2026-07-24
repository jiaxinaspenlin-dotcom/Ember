# Authentication

FastAPI owns the entire authentication flow. There is no Auth.js, no NextAuth,
and no client-side session logic.

## Supported methods

1. **Continue with GitHub** — real OAuth, see [GITHUB_OAUTH.md](GITHUB_OAUTH.md)
2. **Create an account with email and password**
3. **Sign in with email and password**
4. **Sign out**
5. **Reset a forgotten password by email** — see [EMAIL.md](EMAIL.md)
6. **Verify an email address** (optional, opt-in) — see [EMAIL.md](EMAIL.md)

There is no demo account and no seeded user. A reviewer creates a real account
through the sign-up form.

## Sign-up

`POST /api/auth/signup` or the `/signup` form.

1. Email is normalised (trimmed, lowercased) and validated.
2. Display name is collapsed and length-checked (2–120).
3. Password policy: 10–200 characters, at least one letter and one digit.
4. Uniqueness is checked, and the unique index is the real guard — a concurrent
   duplicate raises `IntegrityError` and is returned as
   `EMAIL_ALREADY_REGISTERED`.
5. `User` + `PasswordCredential` + `Profile` are created in one transaction.
6. The role comes from `initial_role_for()` — server-side configuration only.
7. A session is created and the cookie set.
8. A `user.registered` audit event is written.

When `REQUIRE_EMAIL_VERIFICATION=true`, steps 7–8 change: **no session is
issued**, a verification email is sent, and the response is identical whether or
not the address already exists (so signup is not an enumeration oracle). The
account is gated until the address is confirmed. Full detail in
[EMAIL.md](EMAIL.md).

Signup is rate limited per source address (`SIGNUP_MAX_ATTEMPTS_PER_IP`,
default 10 per window), and a rejected duplicate counts towards the budget.
Signup unavoidably reveals that an address is already registered — the
alternative silently breaks the flow — so this throttle is what stops that
response from being an enumeration oracle at scale. See
[SECURITY.md](SECURITY.md#accepted-risks).

## Password hashing

`pwdlib` with the **Argon2** hasher. Plaintext is never stored, never logged,
never returned.

`verify_password(password, None)` still performs a full Argon2 verification
against a dummy hash before returning `False`, so the response time for an
unknown account matches that of a wrong password.

## Sign-in

1. **Rate limit** — two *independent* budgets over the last
   `LOGIN_ATTEMPT_WINDOW_MINUTES`: `LOGIN_MAX_ATTEMPTS` (default 8) failures for
   this account, and `LOGIN_MAX_ATTEMPTS_PER_IP` (default 40) for this source
   address. Either exceeded → `429 LOGIN_RATE_LIMITED`. They are deliberately
   *not* combined into one count: behind a reverse proxy the whole cohort can
   share one address, so a single small budget would let a few failures lock
   everyone out. Attempts live in PostgreSQL, so the limit survives restarts and
   applies across instances. See [SECURITY.md](SECURITY.md).
2. **Lookup and verify** in constant time.
3. **On failure** — record the attempt, write a `user.login_failed` audit event,
   and return the *same* message and status regardless of whether the account
   exists (`INVALID_CREDENTIALS`, "Email or password is incorrect."). Tests
   assert the two responses are byte-identical.
4. **On success** — record the attempt, stamp `last_login_at`, create a session,
   set the cookie.

## Sessions

| Property | Value |
| --- | --- |
| Token | 32 random bytes, URL-safe (`secrets.token_urlsafe`) |
| Storage | **SHA-256 hash only**, in `sessions.token_hash` |
| Cookie | `ember_session`, `HttpOnly`, `SameSite=Lax`, `Secure` in production |
| Lifetime | `SESSION_MAX_AGE_DAYS`, default 30 |
| Revocation | `revoked_at` set on logout; checked on every request |

A high-entropy random token needs no key-stretching, so SHA-256 is the correct
primitive here (unlike passwords, which use Argon2). A database dump cannot be
replayed as a login.

`last_seen_at` is refreshed at most once every five minutes, so an active
session does not cause a write per request.

### Resolution

On every request `resolve_session()` hashes the cookie, joins to the user, and
returns `None` if the row is missing, revoked, expired, or the user is
inactive. Page routes then redirect to `/signin?next=…`; API routes return
`401 SESSION_EXPIRED`.

### After sign-out

- private pages redirect to sign-in
- private API endpoints return 401
- direct messages are not retrievable
- the session row stays, marked `revoked_at`, for the audit trail

Tests cover each of these.

## Changing or resetting a password

`POST /api/auth/password` (signed in) verifies the current password, applies the
policy to the new one, then **revokes every session for that user** — including
the caller — so a stolen session cannot outlive a password change.

`POST /api/auth/password/forgot` and `/api/auth/password/reset` handle the
forgotten-password case by emailing a single-use, hash-at-rest token. Completing
a reset also revokes every session. The forgot endpoint never discloses whether
an address is registered. See [EMAIL.md](EMAIL.md).

## CSRF posture

- The session cookie is `SameSite=Lax`, so a cross-site POST does not carry it.
- Every mutation is POST/PATCH/PUT/DELETE; no state changes on GET.
- OAuth uses a single-use `state` row stored in PostgreSQL.
- The UI and the API share an origin, so there is no permissive CORS policy to
  abuse (no CORS middleware is installed at all).

If Ember is ever split across origins, add a synchroniser token — see
[KNOWN_LIMITATIONS.md](KNOWN_LIMITATIONS.md).

## Roles

Two roles: `member` (default) and `admin`.

A role can be set by exactly three server-side paths:

1. `ADMIN_EMAILS` / `ADMIN_GITHUB_USERNAMES` at **account-creation time only**
2. `ember-admin grant-admin` / `revoke-admin`
3. `PUT /api/admin/users/{id}/role` by an authenticated administrator (audited)

Existing roles are never recalculated at startup, so a deploy cannot silently
promote or demote anyone. Roles cannot be influenced by signup payloads, profile
edits, URL parameters, form values or browser storage; tests assert this
directly.

## What is never exposed

Password hashes, session tokens, OAuth tokens, `ADMIN_*` configuration, and
other users' email addresses. `CurrentUserOut` returns your own email; the
`UserSummary` schema used for everyone else has no email field at all.
