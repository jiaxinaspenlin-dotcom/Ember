# Email: verification and password reset

Ember sends two kinds of transactional email — address verification and
password reset — and does it in a way that never leaks who has an account.

## Delivery backends

| Backend | When | Behaviour |
| --- | --- | --- |
| `smtp` | `SMTP_HOST` and `SMTP_FROM` are set | Real delivery over SMTP (STARTTLS, or implicit TLS on port 465) |
| `console` | SMTP not configured | The **entire message, including the link**, is written to the application log |

`EMAIL_BACKEND=auto` (the default) picks `smtp` when SMTP is configured and
`console` otherwise. The console backend exists so local development is honest:
nothing is silently dropped, and no success is faked. When SMTP *is* configured
but a send fails, the caller is told — a flow never reports success for mail
that did not leave the machine (`app/core/mailer.py`).

Every response that involves email reports `delivered` (did mail actually
leave?) separately from its neutral user-facing message, so a developer on the
console backend is never misled.

## Only the hash is stored

Both flows use `email_tokens`. The row stores the SHA-256 **hash** of the token,
its purpose, the address it was sent to, an expiry, and a `consumed_at` stamp.
The raw token exists only in the emailed link. A database read therefore cannot
be turned into a working link — the same property the session table has.

A token is:

- **single-use** — `consumed_at` is set the moment it is spent
- **expiring** — 24 h for verification, 60 min for reset (both configurable)
- **address-bound** — if the account's email changes after a token is issued,
  the old token stops working
- **superseded on reissue** — requesting a new token retires the previous one

`ember-admin purge-sessions` deletes expired tokens along with expired sessions.

## Password reset

Always available; no configuration required beyond wanting real delivery.

```
POST /api/auth/password/forgot   { email }        -> neutral 200, always
POST /api/auth/password/reset    { token, new_password } -> 200
```

Web pages: `/forgot-password` and `/reset-password?token=…`. The sign-in page
links to the former.

Flow:

1. **Request.** `request_password_reset` responds identically whether or not the
   address exists. For an unknown address it does the same work and sends
   nothing. For a **GitHub-only account** (email but no password) it emails
   "you already have an account, sign in with GitHub" rather than a reset link
   that would do nothing.
2. **Reset.** `reset_password` validates the new password against the policy,
   consumes the token, sets the password, marks the address verified (using the
   link proves mailbox control), and **revokes every existing session** for that
   user — so a stolen session cannot outlive a reset.

Requests are rate limited per source address
(`PASSWORD_RESET_MAX_REQUESTS_PER_IP`, default 10 per window).

## Email verification (opt-in)

Off by default. Turn it on with `REQUIRE_EMAIL_VERIFICATION=true` (which requires
SMTP — the app refuses to boot in production otherwise, so nobody is ever
stranded unable to confirm).

```
POST /api/auth/email/verify   { token }   -> 200, signs the user in
POST /api/auth/email/resend               -> neutral 200 (auth required)
GET  /verify-email/confirm?token=…        -> confirms from the emailed link
```

### What changes when it is on

- **Signup returns no session.** The response is deliberately identical for a new
  and an existing address (`verification_required: true`), so signup stops being
  an enumeration oracle. The real owner of a re-used address is emailed "someone
  tried to sign up as you".
- **Unverified accounts are gated.** They can sign in, but every route except the
  auth endpoints returns `403 EMAIL_NOT_VERIFIED` (API) or redirects to
  `/verify-email` (pages), so they can resend, confirm, or sign out — nothing
  else.
- **Confirming signs the user in** and sends them to profile completion.

Accounts with no email address (GitHub sign-in where the address is private) are
never gated — there is nothing to confirm.

### What changes when it is off (the default)

Signup behaves as before: a duplicate raises `EMAIL_ALREADY_REGISTERED`, the
account is usable immediately, and — if SMTP happens to be configured — a
confirmation email is still sent so people *can* verify, but nothing depends on
it.

## Non-disclosure summary

| Endpoint | With verification on | With it off |
| --- | --- | --- |
| Signup, existing address | identical to a new signup; owner emailed | `409 EMAIL_ALREADY_REGISTERED` |
| Forgot password, unknown address | identical neutral response | identical neutral response |
| Forgot password, GitHub-only account | neutral; "sign in with GitHub" email | same |

## SMTP setup

```bash
EMAIL_BACKEND=smtp            # or leave as auto
SMTP_HOST=smtp.postmarkapp.com
SMTP_PORT=587
SMTP_USERNAME=your-token
SMTP_PASSWORD=your-token
SMTP_FROM=ember@your-domain
SMTP_FROM_NAME=Ember
SMTP_USE_TLS=true
```

Any transactional provider works (Postmark, SendGrid, SES, Resend via SMTP,
Mailgun). Port 465 switches to implicit TLS automatically. Configure SPF/DKIM on
`SMTP_FROM`'s domain so links do not land in spam.

## Testing

`app/tests/test_email_flows.py` (20 tests) captures outbound mail with a fixture
instead of delivering it, then drives both flows end to end: token issuance and
hashing, single-use, session revocation on reset, neutral responses for unknown
addresses, GitHub-only guidance, the verification gate, non-disclosure of
existing addresses, address-change invalidation, and every rendered page. It
runs on the console backend so no test touches a real mail server.
