"""Account lifecycle: registration, sign-in, OAuth linking, role assignment.

Every rule here runs in Python.  No account is ever created outside of a real
user action (signup, or a completed GitHub OAuth flow).
"""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session as DbSession

from app.auth import passwords
from app.core.config import settings
from app.core.enums import AuditAction
from app.core.errors import (
    AuthenticationError,
    ConflictError,
    NotFoundError,
    RateLimitedError,
    ValidationError,
)
from app.core.security import encrypt_secret, normalize_email
from app.db.base import utcnow
from app.models.user import (
    LoginAttempt,
    OAuthAccount,
    PasswordCredential,
    User,
)
from app.services import audit

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
GENERIC_LOGIN_FAILURE = "Email or password is incorrect."

# Signup throttling shares the login_attempts table under a reserved identifier.
# It cannot collide with a login identifier because those are always normalised
# email addresses, which always contain an "@". (PostgreSQL text rejects NUL
# bytes, so a control-character sentinel is not an option.)
SIGNUP_IDENTIFIER = "[signup]"


@dataclass(slots=True)
class GitHubIdentity:
    """The identity fields Ember consumes from GitHub."""

    provider_account_id: str
    username: str
    display_name: str | None
    avatar_url: str | None
    email: str | None
    email_verified: bool
    access_token: str | None = None
    scopes: str | None = None


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def validate_email(email: str) -> str:
    normalized = normalize_email(email)
    if not normalized:
        raise ValidationError("Email address is required.", details={"field": "email"})
    if len(normalized) > 320 or not EMAIL_PATTERN.match(normalized):
        raise ValidationError("Enter a valid email address.", details={"field": "email"})
    return normalized


def validate_display_name(display_name: str) -> str:
    cleaned = " ".join(display_name.split())
    if len(cleaned) < 2:
        raise ValidationError(
            "Display name must be at least 2 characters.", details={"field": "display_name"}
        )
    if len(cleaned) > 120:
        raise ValidationError(
            "Display name must be at most 120 characters.", details={"field": "display_name"}
        )
    return cleaned


# ---------------------------------------------------------------------------
# Role assignment (server-side only)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


def get_user(db: DbSession, user_id: uuid.UUID) -> User | None:
    return db.get(User, user_id)


def require_user(db: DbSession, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise NotFoundError("Member not found.", code="USER_NOT_FOUND")
    return user


def find_by_email(db: DbSession, email: str) -> User | None:
    return db.scalar(select(User).where(User.email == normalize_email(email)))


def find_oauth_account(
    db: DbSession, provider: str, provider_account_id: str
) -> OAuthAccount | None:
    return db.scalar(
        select(OAuthAccount).where(
            OAuthAccount.provider == provider,
            OAuthAccount.provider_account_id == provider_account_id,
        )
    )


def user_count(db: DbSession) -> int:
    return int(db.scalar(select(func.count()).select_from(User)) or 0)


# ---------------------------------------------------------------------------
# Registration and sign-in
# ---------------------------------------------------------------------------


def register_with_password(
    db: DbSession,
    *,
    email: str,
    password: str,
    display_name: str,
    ip_address: str | None = None,
) -> User:
    """Create a real account from the signup form."""

    _enforce_signup_rate_limit(db, ip_address)

    normalized_email = validate_email(email)
    clean_name = validate_display_name(display_name)
    passwords.validate_password_strength(password)

    # Signup necessarily reveals that an address is already registered, so the
    # rate limit above is what stops that from being enumerated at scale.
    if find_by_email(db, normalized_email) is not None:
        _log_attempt(db, SIGNUP_IDENTIFIER, ip_address, successful=False)
        raise ConflictError(
            "An account with that email already exists. Try signing in instead.",
            code="EMAIL_ALREADY_REGISTERED",
        )

    user = User(
        email=normalized_email,
        email_verified=False,
        display_name=clean_name,
    )
    db.add(user)
    db.flush()

    db.add(PasswordCredential(user_id=user.id, password_hash=passwords.hash_password(password)))
    _log_attempt(db, SIGNUP_IDENTIFIER, ip_address, successful=True)
    audit.record(
        db,
        AuditAction.USER_REGISTERED,
        actor_id=user.id,
        entity_type="user",
        entity_id=user.id,
        context={"method": "password"},
        ip_address=ip_address,
    )
    try:
        db.flush()
    except IntegrityError as exc:  # pragma: no cover - race with a concurrent signup
        db.rollback()
        raise ConflictError(
            "An account with that email already exists. Try signing in instead.",
            code="EMAIL_ALREADY_REGISTERED",
        ) from exc
    return user


@dataclass(slots=True)
class SignupOutcome:
    """What signup did, and how much of it the caller may reveal.

    With verification enabled the response must look identical whether or not
    the address was already registered, so ``user`` is ``None`` for a duplicate
    and ``disclosed`` is ``False``.
    """

    user: User | None
    verification_sent: bool
    disclosed: bool

    @property
    def created(self) -> bool:
        return self.user is not None


def email_verification_pending(user: User) -> bool:
    """True when this account must confirm its address before using Ember.

    An account with no email address (GitHub sign-in where the address is
    private) has nothing to confirm and is never blocked.
    """

    if not settings.require_email_verification:
        return False
    return bool(user.email) and not user.email_verified


def register_account(
    db: DbSession,
    *,
    email: str,
    password: str,
    display_name: str,
    ip_address: str | None = None,
) -> SignupOutcome:
    """Create an account under the configured verification policy.

    * verification **on**  -- duplicates are not disclosed; the real owner gets a
      "you already have an account" email instead, and new accounts must confirm
      before signing in.
    * verification **off** -- a duplicate raises ``EMAIL_ALREADY_REGISTERED`` as
      before, and the account is usable immediately.
    """

    from app.core import mailer
    from app.services import credentials

    if not settings.require_email_verification:
        user = register_with_password(
            db,
            email=email,
            password=password,
            display_name=display_name,
            ip_address=ip_address,
        )
        sent = False
        if settings.email_delivery_enabled:
            # Nothing is gated on it, but people can still confirm their address.
            credentials.send_verification_email(db, user=user, ip_address=ip_address)
            sent = True
        return SignupOutcome(user=user, verification_sent=sent, disclosed=True)

    # --- verification required: the response must not reveal the outcome ---
    _enforce_signup_rate_limit(db, ip_address)
    normalized_email = validate_email(email)
    validate_display_name(display_name)
    passwords.validate_password_strength(password)

    existing = find_by_email(db, normalized_email)
    if existing is not None:
        _log_attempt(db, SIGNUP_IDENTIFIER, ip_address, successful=False)
        mailer.send(
            mailer.signup_attempt_email(
                to=normalized_email,
                display_name=existing.display_name,
                sign_in_link=f"{settings.frontend_url.rstrip('/')}/signin",
            )
        )
        db.flush()
        return SignupOutcome(user=None, verification_sent=True, disclosed=False)

    user = register_with_password(
        db,
        email=email,
        password=password,
        display_name=display_name,
        ip_address=ip_address,
    )
    credentials.send_verification_email(db, user=user, ip_address=ip_address)
    return SignupOutcome(user=user, verification_sent=True, disclosed=False)


def _window_start() -> dt.datetime:
    return utcnow() - dt.timedelta(minutes=settings.login_attempt_window_minutes)


def _failed_attempts_for_identifier(db: DbSession, identifier: str) -> int:
    return int(
        db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.created_at >= _window_start(),
                LoginAttempt.successful.is_(False),
                LoginAttempt.identifier == identifier,
            )
        )
        or 0
    )


def _failed_attempts_for_ip(db: DbSession, ip_address: str | None) -> int:
    if not ip_address:
        return 0
    return int(
        db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.created_at >= _window_start(),
                LoginAttempt.successful.is_(False),
                LoginAttempt.ip_address == ip_address,
            )
        )
        or 0
    )


def _enforce_signup_rate_limit(db: DbSession, ip_address: str | None) -> None:
    """Throttle account creation per source address.

    Signup unavoidably reveals whether an address is already registered (the
    alternative is a silent no-op that breaks the product).  Rate limiting is
    therefore what keeps that from being enumerable at scale, and it also stops
    bulk account creation.
    """

    if not ip_address:
        return
    recent = int(
        db.scalar(
            select(func.count())
            .select_from(LoginAttempt)
            .where(
                LoginAttempt.created_at >= _window_start(),
                LoginAttempt.identifier == SIGNUP_IDENTIFIER,
                LoginAttempt.ip_address == ip_address,
            )
        )
        or 0
    )
    if recent >= settings.signup_max_attempts_per_ip:
        raise RateLimitedError(
            "Too many accounts created from this location. Try again later.",
            code="SIGNUP_RATE_LIMITED",
        )


def _enforce_login_rate_limit(db: DbSession, identifier: str, ip_address: str | None) -> None:
    """Two independent budgets, never a single combined one.

    The per-account budget stops brute force against one person.  The per-IP
    budget is deliberately much larger, because behind a reverse proxy every
    member can share one source address -- OR-ing the two together would let
    eight failures anywhere lock out the whole cohort.
    """

    if _failed_attempts_for_identifier(db, identifier) >= settings.login_max_attempts:
        raise RateLimitedError(
            "Too many sign-in attempts for this account. Wait a few minutes and try again.",
            code="LOGIN_RATE_LIMITED",
        )
    if _failed_attempts_for_ip(db, ip_address) >= settings.login_max_attempts_per_ip:
        raise RateLimitedError(
            "Too many sign-in attempts from this location. Wait a few minutes and try again.",
            code="LOGIN_RATE_LIMITED",
        )


def _log_attempt(
    db: DbSession, identifier: str, ip_address: str | None, *, successful: bool
) -> None:
    db.add(
        LoginAttempt(
            identifier=identifier[:320], ip_address=ip_address, successful=successful
        )
    )
    db.flush()


def authenticate_with_password(
    db: DbSession, *, email: str, password: str, ip_address: str | None = None
) -> User:
    """Verify credentials. Failures are deliberately indistinguishable."""

    identifier = normalize_email(email)
    _enforce_login_rate_limit(db, identifier, ip_address)

    user = db.scalar(select(User).where(User.email == identifier))
    credential = (
        db.scalar(select(PasswordCredential).where(PasswordCredential.user_id == user.id))
        if user is not None
        else None
    )

    password_ok = passwords.verify_password(
        password, credential.password_hash if credential else None
    )
    if user is None or credential is None or not password_ok or not user.is_active:
        _log_attempt(db, identifier, ip_address, successful=False)
        audit.record(
            db,
            AuditAction.USER_LOGIN_FAILED,
            actor_id=user.id if user else None,
            entity_type="user",
            entity_id=user.id if user else None,
            context={"method": "password"},
            ip_address=ip_address,
        )
        raise AuthenticationError(GENERIC_LOGIN_FAILURE, code="INVALID_CREDENTIALS")

    _log_attempt(db, identifier, ip_address, successful=True)
    user.last_login_at = utcnow()
    audit.record(
        db,
        AuditAction.USER_LOGGED_IN,
        actor_id=user.id,
        entity_type="user",
        entity_id=user.id,
        context={"method": "password"},
        ip_address=ip_address,
    )
    db.flush()
    return user


def change_password(
    db: DbSession, *, user: User, current_password: str, new_password: str
) -> None:
    credential = db.scalar(
        select(PasswordCredential).where(PasswordCredential.user_id == user.id)
    )
    if credential is None:
        raise ValidationError(
            "This account signs in with GitHub and has no password set.",
            code="NO_PASSWORD_CREDENTIAL",
        )
    if not passwords.verify_password(current_password, credential.password_hash):
        raise AuthenticationError(
            "Current password is incorrect.", code="INVALID_CREDENTIALS"
        )
    passwords.validate_password_strength(new_password)
    credential.password_hash = passwords.hash_password(new_password)
    db.flush()


def set_initial_password(db: DbSession, *, user: User, new_password: str) -> None:
    """Let a GitHub-only account add an email/password credential."""

    existing = db.scalar(
        select(PasswordCredential).where(PasswordCredential.user_id == user.id)
    )
    if existing is not None:
        raise ConflictError(
            "This account already has a password.", code="PASSWORD_ALREADY_SET"
        )
    if not user.email:
        raise ValidationError(
            "Add an email address to your account before setting a password.",
            code="EMAIL_REQUIRED",
        )
    passwords.validate_password_strength(new_password)
    db.add(
        PasswordCredential(user_id=user.id, password_hash=passwords.hash_password(new_password))
    )
    db.flush()


# ---------------------------------------------------------------------------
# GitHub OAuth account resolution
# ---------------------------------------------------------------------------


def resolve_github_user(
    db: DbSession, identity: GitHubIdentity, *, ip_address: str | None = None
) -> tuple[User, bool]:
    """Find or create the user behind a GitHub identity.

    Returns ``(user, created)``.  Linking rules:

    * A known ``(provider, provider_account_id)`` always wins -- it is stable.
    * Otherwise, a *verified* GitHub email matching an existing account links to
      it.  Unverified emails never auto-link, so two unverified addresses can
      never merge accounts.
    * Otherwise a new account is created.

    Profile fields the user has edited are never overwritten on later logins.
    """

    existing_link = find_oauth_account(db, "github", identity.provider_account_id)
    if existing_link is not None:
        user = db.get(User, existing_link.user_id)
        if user is None or not user.is_active:
            raise AuthenticationError(
                "This account is no longer active.", code="ACCOUNT_INACTIVE"
            )
        _refresh_oauth_account(db, existing_link, identity)
        _fill_missing_identity_fields(user, identity)
        user.last_login_at = utcnow()
        audit.record(
            db,
            AuditAction.USER_LOGGED_IN,
            actor_id=user.id,
            entity_type="user",
            entity_id=user.id,
            context={"method": "github"},
            ip_address=ip_address,
        )
        db.flush()
        return user, False

    user = None
    if identity.email and identity.email_verified:
        user = find_by_email(db, identity.email)

    created = False
    if user is None:
        normalized_email = (
            normalize_email(identity.email)
            if identity.email and identity.email_verified
            else None
        )
        user = User(
            email=normalized_email,
            email_verified=bool(normalized_email),
            display_name=validate_display_name(
                identity.display_name or identity.username or "New member"
            ),
            avatar_url=identity.avatar_url,
        )
        db.add(user)
        db.flush()
        created = True
        audit.record(
            db,
            AuditAction.USER_REGISTERED,
            actor_id=user.id,
            entity_type="user",
            entity_id=user.id,
            context={"method": "github"},
            ip_address=ip_address,
        )
    else:
        _fill_missing_identity_fields(user, identity)

    link = OAuthAccount(
        user_id=user.id,
        provider="github",
        provider_account_id=identity.provider_account_id,
        provider_username=identity.username,
        provider_email=identity.email,
        scopes=identity.scopes,
    )
    _apply_token(link, identity)
    db.add(link)
    audit.record(
        db,
        AuditAction.OAUTH_ACCOUNT_LINKED,
        actor_id=user.id,
        entity_type="oauth_account",
        entity_id=user.id,
        context={"provider": "github"},
        ip_address=ip_address,
    )
    user.last_login_at = utcnow()
    if not created:
        audit.record(
            db,
            AuditAction.USER_LOGGED_IN,
            actor_id=user.id,
            entity_type="user",
            entity_id=user.id,
            context={"method": "github"},
            ip_address=ip_address,
        )
    try:
        db.flush()
    except IntegrityError as exc:  # pragma: no cover - concurrent link
        db.rollback()
        raise ConflictError(
            "That GitHub account is already linked to another Ember account.",
            code="OAUTH_ACCOUNT_TAKEN",
        ) from exc
    return user, created


def _apply_token(link: OAuthAccount, identity: GitHubIdentity) -> None:
    if settings.store_github_tokens and identity.access_token:
        link.access_token_encrypted = encrypt_secret(identity.access_token)
    else:
        link.access_token_encrypted = None


def _refresh_oauth_account(
    db: DbSession, link: OAuthAccount, identity: GitHubIdentity
) -> None:
    link.provider_username = identity.username
    link.provider_email = identity.email
    link.scopes = identity.scopes
    _apply_token(link, identity)
    db.flush()


def _fill_missing_identity_fields(user: User, identity: GitHubIdentity) -> None:
    """Only fill blanks -- user-edited values are never overwritten."""

    if not user.avatar_url and identity.avatar_url:
        user.avatar_url = identity.avatar_url
    if not user.email and identity.email and identity.email_verified:
        user.email = normalize_email(identity.email)
        user.email_verified = True
