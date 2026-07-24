# GitHub OAuth

Real OAuth, executed by the FastAPI backend. Nothing is simulated.

## The flow

```
Browser                    Ember backend                    GitHub
   │                             │                             │
   │ click "Continue with GitHub"│                             │
   ├────────────────────────────►│                             │
   │                             │ 1. insert single-use state  │
   │                             │    row in PostgreSQL        │
   │      302 to GitHub          │                             │
   │◄────────────────────────────┤                             │
   ├─────────────────────────────┼────────────────────────────►│
   │                             │        user authorises      │
   │◄────────────────────────────┼─────────────────────────────┤
   │  GET /api/auth/github/callback?code=…&state=…             │
   ├────────────────────────────►│                             │
   │                             │ 2. validate + consume state │
   │                             │ 3. POST code → access token │
   │                             ├────────────────────────────►│
   │                             │ 4. GET /user, /user/emails  │
   │                             ├────────────────────────────►│
   │                             │ 5. create/link User,        │
   │                             │    OAuthAccount, Profile    │
   │                             │ 6. create session + cookie  │
   │       302 into Ember        │                             │
   │◄────────────────────────────┤                             │
```

Implementation: `app/auth/github.py` (protocol) and
`app/services/accounts.py::resolve_github_user` (identity rules).

## Scopes

Requested: **`read:user user:email`** — and nothing else.

Never requested: repository read or write, organization administration, issue
write, code write, gists, workflow.

`user:email` is requested so that GitHub accounts with a private profile email
can still be linked to an existing Ember account. Drop it by setting
`GITHUB_OAUTH_SCOPES=read:user`; sign-in still works, and such accounts simply
have no email until the user adds one.

## The `state` parameter

`state` is 24 random bytes stored as an `oauth_states` row with a 10-minute TTL
and a `consumed_at` stamp. Validation requires a row that exists, is unexpired,
and has not been consumed; consumption is immediate.

State lives in PostgreSQL rather than process memory so it works across
restarts, multiple instances and rolling deploys. `ember-admin purge-sessions`
clears expired rows.

## Provider tokens

By default (`STORE_GITHUB_TOKENS=false`) the access token is used to read the
profile during the callback and then **discarded** — Ember has no ongoing need
for GitHub API access.

If you set `STORE_GITHUB_TOKENS=true`, the token is Fernet-encrypted with a key
derived from `SESSION_SECRET` before being written to
`oauth_accounts.access_token_encrypted`. It is never serialised into any
response, never included in a session payload, and never logged. A test asserts
that the ciphertext does not contain the plaintext and that no schema emits it.

## Account linking

`resolve_github_user()` applies these rules, in order:

1. **A known `(provider, provider_account_id)` wins.** This is GitHub's stable
   numeric account id — it survives username changes.
2. **Otherwise, a *verified* GitHub email that matches an existing account links
   to it.** The user is signed into the account they already had.
3. **Otherwise a new account is created**, with a Profile, using the GitHub
   display name (falling back to the username) and avatar.

**Two unverified email values never merge accounts.** If GitHub reports an
unverified address that happens to match an existing Ember user, a *separate*
account is created. There is a test for exactly this.

### First-time GitHub login

Creates `User`, `OAuthAccount` and `Profile`, then redirects to
`/profile/complete` where the member adds skills, their current project and a
working status.

### Returning login

Reuses the account, refreshes provider metadata, and **only fills blanks**:

```python
if not user.avatar_url and identity.avatar_url:
    user.avatar_url = identity.avatar_url
```

A display name or avatar the user edited in Ember is never overwritten by a
later GitHub login.

## Handled edge cases

| Situation | Behaviour |
| --- | --- |
| GitHub returns no email | Account created with `email = NULL`; the user can add one later |
| Email is private/unverified | Never auto-links; a new account is created |
| User cancels on GitHub | `?error=access_denied` → `/signin?error=github_cancelled` |
| `state` missing, expired or replayed | `/signin?error=github_state_invalid` |
| GitHub access revoked mid-flow | 401 from the API → `/signin?error=github_access_revoked` |
| GitHub unreachable / rate limited | `/signin?error=github_unreachable`, retryable |
| The GitHub account is already linked elsewhere | `/signin?error=github_link_conflict` |
| OAuth not configured | The button is not rendered; direct calls return 503 |

Each `error` value maps to a human-readable sentence on the sign-in page.

## Setting up the OAuth app

<https://github.com/settings/developers> → **New OAuth App**.

**Local**

| Field | Value |
| --- | --- |
| Homepage URL | `http://localhost:8000` |
| Authorization callback URL | `http://localhost:8000/api/auth/github/callback` |

**Production**

| Field | Value |
| --- | --- |
| Homepage URL | `https://YOUR-DOMAIN` |
| Authorization callback URL | `https://YOUR-DOMAIN/api/auth/github/callback` |

Then set:

```bash
GITHUB_CLIENT_ID=Iv1.xxxxxxxxxxxx
GITHUB_CLIENT_SECRET=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GITHUB_OAUTH_REDIRECT_URI=https://YOUR-DOMAIN/api/auth/github/callback
```

**The callback URL must match exactly** — scheme, host, port and path. GitHub
compares it against both the registered app *and* the `redirect_uri` sent during
the code exchange. A trailing slash or `http` instead of `https` will fail with
`redirect_uri_mismatch`.

Use **two separate OAuth apps** (one local, one production) rather than trying to
register multiple callbacks on one.

## Redirecting back into Ember

The callback ends with a 302 to `FRONTEND_URL` plus a path:

- brand-new account, or profile not yet completed → `/profile/complete`
- otherwise → the `redirect_to` captured at the start (validated to begin with
  `/`, so it cannot be used as an open redirect), defaulting to `/`

Because the UI and API share an origin, `FRONTEND_URL` and `BACKEND_URL` are the
same value in a normal deployment.

## Automated coverage

`app/tests/test_github_oauth_flow.py` drives the whole handshake through the real
backend routes against a **mocked HTTP transport** — the authorize redirect, the
DB-stored single-use `state`, the code exchange, the identity fetch, first-login
account creation, returning-login reuse, verified-email linking, replayed and
unknown `state`, cancellation, and token encryption. Only the raw calls to
`github.com` are stubbed; everything else is the code that runs in production.

## Manual verification (against the real github.com)

1. Set the four `GITHUB_*` variables and restart.
2. `GET /api/auth/github/status` → `{"enabled": true}`.
3. Visit `/signin` — the GitHub button is rendered.
4. Complete the flow; you land on `/profile/complete`.
5. Check the database:

```sql
select provider, provider_username, access_token_encrypted is null as token_discarded
from oauth_accounts;
select consumed_at is not null as state_consumed from oauth_states;
```

6. Sign out and sign in again with GitHub: no duplicate user is created.
