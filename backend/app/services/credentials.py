"""Email verification and password reset.

Both flows share one shape:

1. issue a single-use, expiring token (only its hash is stored)
2. email a link containing the raw token
3. consume the token, apply the effect, invalidate every sibling token

Neither endpoint discloses whether an address is registered.  A request for an
unknown address does the same work, takes the same time and returns the same
response as one for a real account.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session as DbSession

from app.auth import passwords, sessions
from app.core import mailer
from app.core.config import settings
from app.core.enums import AuditAction, EmailTokenPurpose
from app.core.errors import RateLimitedError, ValidationError
from app.core.security import generate_token, hash_token, normalize_email
from app.db.base import utcnow
from app.models.user import EmailToken, LoginAttempt, PasswordCredential, User
from app.services import audit

# Reserved identifiers for the shared rate-limit table. They cannot collide with
# login identifiers, which are always normalised email addresses containing "@".
RESET_IDENTIFIER = "[password-reset]"
VERIFY_IDENTIFIER = "[email-verify]"


@dataclass(slots=True)
class TokenIssue:
    """A freshly minted token and how its email fared."""

    raw_token: str
    link: str
    delivery: mailer.DeliveryResult


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


def _window_start() -> dt.datetime:
    return utcnow() - dt.timedelta(minutes=settings.login_attempt_window_minutes)


def _enforce_ip_budget(
    db: DbSession, *, identifier: str, ip_address: str | None, limit: int, message: str
) -> None:
    if not ip_address:
        return
    recent = int(
        db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.created_at >= _window_start(),
                LoginAttempt.identifier == identifier,
                LoginAttempt.ip_address == ip_address,
            )
        )
        or 0
    )
    if recent >= limit:
        raise RateLimitedError(message, code="EMAIL_REQUEST_RATE_LIMITED")


def _record_request(db: DbSession, identifier: str, ip_address: str | None) -> None:
    db.add(LoginAttempt(identifier=identifier, ip_address=ip_address, successful=True))
    db.flush()


# ---------------------------------------------------------------------------
# Token lifecycle
# ---------------------------------------------------------------------------


def _ttl_for(purpose: EmailTokenPurpose) -> dt.timedelta:
    if purpose is EmailTokenPurpose.VERIFY_EMAIL:
        return dt.timedelta(hours=settings.email_verification_ttl_hours)
    return dt.timedelta(minutes=settings.password_reset_ttl_minutes)


def _invalidate_outstanding(
    db: DbSession, *, user_id: uuid.UUID, purpose: EmailTokenPurpose
) -> None:
    """Issuing a new token retires any earlier one for the same purpose."""

    db.execute(
        update(EmailToken)
        .where(
            EmailToken.user_id == user_id,
            EmailToken.purpose == purpose,
            EmailToken.consumed_at.is_(None),
        )
        .values(consumed_at=utcnow())
    )
    db.flush()


def issue_token(
    db: DbSession,
    *,
    user: User,
    purpose: EmailTokenPurpose,
    ip_address: str | None = None,
) -> str:
    """Create a token, store only its hash, and return the raw value."""

    if not user.email:
        raise ValidationError(
            "This account has no email address.", code="EMAIL_REQUIRED"
        )
    _invalidate_outstanding(db, user_id=user.id, purpose=purpose)
    raw_token = generate_token()
    db.add(
        EmailToken(
            user_id=user.id,
            purpose=purpose,
            token_hash=hash_token(raw_token),
            email=user.email,
            expires_at=utcnow() + _ttl_for(purpose),
            requested_ip=ip_address,
        )
    )
    db.flush()
    return raw_token


def consume_token(db: DbSession, *, raw_token: str, purpose: EmailTokenPurpose) -> EmailToken:
    """Validate and single-use a token. Raises for anything not usable."""

    invalid = ValidationError(
        "That link is invalid or has expired. Request a new one.",
        code="TOKEN_INVALID",
    )
    if not raw_token:
        raise invalid
    token = db.scalar(
        select(EmailToken).where(
            EmailToken.token_hash == hash_token(raw_token), EmailToken.purpose == purpose
        )
    )
    now = utcnow()
    if token is None or token.consumed_at is not None or token.expires_at <= now:
        raise invalid

    user = db.get(User, token.user_id)
    if user is None or not user.is_active:
        raise invalid
    # A token is bound to the address it was sent to; changing the address
    # afterwards must not leave a usable link behind.
    if user.email != token.email:
        raise invalid

    token.consumed_at = now
    db.flush()
    return token


def purge_expired_tokens(db: DbSession) -> int:
    from sqlalchemy import delete

    from app.db.base import rows_affected

    return rows_affected(
        db.execute(delete(EmailToken).where(EmailToken.expires_at < utcnow()))
    )


# ---------------------------------------------------------------------------
# Email verification
# ---------------------------------------------------------------------------


def _verification_link(raw_token: str) -> str:
    return f"{settings.frontend_url.rstrip('/')}/verify-email/confirm?token={raw_token}"


def send_verification_email(
    db: DbSession, *, user: User, ip_address: str | None = None
) -> TokenIssue:
    """Issue a verification token and email it."""

    if user.email_verified:
        raise ValidationError(
            "This address is already confirmed.", code="EMAIL_ALREADY_VERIFIED"
        )
    _enforce_ip_budget(
        db,
        identifier=VERIFY_IDENTIFIER,
        ip_address=ip_address,
        limit=settings.verification_max_requests_per_ip,
        message="Too many verification emails requested. Try again later.",
    )

    raw_token = issue_token(
        db, user=user, purpose=EmailTokenPurpose.VERIFY_EMAIL, ip_address=ip_address
    )
    link = _verification_link(raw_token)
    assert user.email is not None  # issue_token guarantees this
    delivery = mailer.send(
        mailer.verification_email(
            to=user.email,
            display_name=user.display_name,
            link=link,
            ttl_hours=settings.email_verification_ttl_hours,
        )
    )
    _record_request(db, VERIFY_IDENTIFIER, ip_address)
    audit.record(
        db,
        AuditAction.EMAIL_VERIFICATION_SENT,
        actor_id=user.id,
        entity_type="user",
        entity_id=user.id,
        context={"backend": delivery.backend},
        ip_address=ip_address,
    )
    db.flush()
    return TokenIssue(raw_token=raw_token, link=link, delivery=delivery)


def verify_email(db: DbSession, *, raw_token: str, ip_address: str | None = None) -> User:
    token = consume_token(db, raw_token=raw_token, purpose=EmailTokenPurpose.VERIFY_EMAIL)
    user = db.get(User, token.user_id)
    assert user is not None  # consume_token validated it

    user.email_verified = True
    audit.record(
        db,
        AuditAction.EMAIL_VERIFIED,
        actor_id=user.id,
        entity_type="user",
        entity_id=user.id,
        ip_address=ip_address,
    )
    db.flush()
    return user


# ---------------------------------------------------------------------------
# Password reset
# ---------------------------------------------------------------------------


def _reset_link(raw_token: str) -> str:
    return f"{settings.frontend_url.rstrip('/')}/reset-password?token={raw_token}"


def request_password_reset(
    db: DbSession, *, email: str, ip_address: str | None = None
) -> TokenIssue | None:
    """Start a password reset.

    Returns ``None`` when there is nothing to send (unknown address, or an
    account that signs in with GitHub only).  Callers **must** respond
    identically either way -- the neutral response is the whole point.
    """

    _enforce_ip_budget(
        db,
        identifier=RESET_IDENTIFIER,
        ip_address=ip_address,
        limit=settings.password_reset_max_requests_per_ip,
        message="Too many password reset requests. Try again later.",
    )
    _record_request(db, RESET_IDENTIFIER, ip_address)

    normalized = normalize_email(email)
    user = db.scalar(select(User).where(User.email == normalized))
    if user is None or not user.is_active:
        db.flush()
        return None

    audit.record(
        db,
        AuditAction.PASSWORD_RESET_REQUESTED,
        actor_id=user.id,
        entity_type="user",
        entity_id=user.id,
        ip_address=ip_address,
    )

    has_password = (
        db.scalar(select(PasswordCredential).where(PasswordCredential.user_id == user.id))
        is not None
    )
    if not has_password:
        # A GitHub-only account has no password to reset. Tell the owner how to
        # get in rather than sending a link that would do nothing.
        delivery = mailer.send(
            mailer.signup_attempt_email(
                to=normalized,
                display_name=user.display_name,
                sign_in_link=f"{settings.frontend_url.rstrip('/')}/signin",
            )
        )
        db.flush()
        return TokenIssue(raw_token="", link="", delivery=delivery)

    raw_token = issue_token(
        db, user=user, purpose=EmailTokenPurpose.RESET_PASSWORD, ip_address=ip_address
    )
    link = _reset_link(raw_token)
    delivery = mailer.send(
        mailer.password_reset_email(
            to=normalized,
            display_name=user.display_name,
            link=link,
            ttl_minutes=settings.password_reset_ttl_minutes,
        )
    )
    db.flush()
    return TokenIssue(raw_token=raw_token, link=link, delivery=delivery)


def reset_password(
    db: DbSession, *, raw_token: str, new_password: str, ip_address: str | None = None
) -> User:
    """Complete a reset: set the password and sign every session out."""

    passwords.validate_password_strength(new_password)
    token = consume_token(db, raw_token=raw_token, purpose=EmailTokenPurpose.RESET_PASSWORD)
    user = db.get(User, token.user_id)
    assert user is not None

    credential = db.scalar(
        select(PasswordCredential).where(PasswordCredential.user_id == user.id)
    )
    if credential is None:
        credential = PasswordCredential(
            user_id=user.id, password_hash=passwords.hash_password(new_password)
        )
        db.add(credential)
    else:
        credential.password_hash = passwords.hash_password(new_password)

    # Completing a reset from a link proves control of the mailbox.
    if not user.email_verified:
        user.email_verified = True

    # Anyone already holding a session for this account loses it.
    sessions.revoke_all_sessions_for_user(db, user.id)

    audit.record(
        db,
        AuditAction.PASSWORD_RESET_COMPLETED,
        actor_id=user.id,
        entity_type="user",
        entity_id=user.id,
        ip_address=ip_address,
    )
    db.flush()
    return user
