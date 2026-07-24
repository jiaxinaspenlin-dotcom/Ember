"""Application configuration.

All configuration comes from environment variables (or a local .env file during
development).  Nothing here contains application *data* -- only deployment
settings and the immutable enum-like lists that describe permitted system states.
"""

from __future__ import annotations

import functools
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]

# Sentinel default. Booting with this value in production is refused outright.
INSECURE_SESSION_SECRET = "dev-insecure-session-secret-change-me"


class Settings(BaseSettings):
    """Runtime settings for the Ember backend."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Core -------------------------------------------------------------
    environment: Environment = "development"
    database_url: str = Field(
        default="postgresql+psycopg://localhost:5432/ember_dev",
        description="SQLAlchemy database URL. Must point at PostgreSQL.",
    )
    session_secret: str = Field(
        default=INSECURE_SESSION_SECRET,
        min_length=16,
        description="Secret used for signing/encrypting server-side artefacts.",
    )

    # --- URLs -------------------------------------------------------------
    frontend_url: str = "http://localhost:8000"
    backend_url: str = "http://localhost:8000"

    # --- GitHub OAuth -----------------------------------------------------
    github_client_id: str = ""
    github_client_secret: str = ""
    github_oauth_redirect_uri: str = "http://localhost:8000/api/auth/github/callback"
    github_oauth_scopes: str = "read:user user:email"
    store_github_tokens: bool = False

    # --- Sessions ---------------------------------------------------------
    session_cookie_name: str = "ember_session"
    session_max_age_days: int = 30
    session_cookie_domain: str | None = None

    # --- Client behaviour -------------------------------------------------
    polling_interval_ms: int = 4000

    # --- Admin bootstrap (server-side only, never sent to the browser) -----
    admin_github_usernames: str = ""
    admin_emails: str = ""

    # --- Cohorts (tenants) ------------------------------------------------
    # Demo default: anyone signed in can discover and join any cohort with one
    # click (no invite needed). Set false when shipped so joining requires a
    # cohort invite link.
    cohort_open_join: bool = True
    # How many cohorts one account may create. Caps the open-join spam surface;
    # 0 disables the limit. Existing memberships are never affected.
    max_cohorts_created_per_user: int = 10

    # --- Startup / resilience ---------------------------------------------
    # Boot waits for the database instead of crashing if it is briefly not
    # ready (common when the app container starts before Postgres accepts
    # connections). Bounded so a genuinely-down database still fails loudly.
    db_connect_max_attempts: int = 10
    db_connect_backoff_seconds: float = 1.0

    # --- Rate limiting ----------------------------------------------------
    # Per-account budget: stops brute force against one person.
    login_max_attempts: int = 8
    # Per-source-address budget: deliberately much larger, because behind a
    # reverse proxy the whole cohort can share one address.
    login_max_attempts_per_ip: int = 40
    signup_max_attempts_per_ip: int = 10
    login_attempt_window_minutes: int = 15

    # --- Proxy ------------------------------------------------------------
    # Only trust X-Forwarded-For when a trusted proxy actually sets it.
    # Enable this on Render / Fly / Railway; leave it off when the app is
    # directly exposed, where the header is attacker-controlled.
    trust_proxy_headers: bool = False

    # --- Email delivery ---------------------------------------------------
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_from_name: str = "Ember"
    smtp_use_tls: bool = True
    smtp_timeout_seconds: int = 15

    # Where mail goes when SMTP is not configured. "console" writes the whole
    # message (including the link) to the application log, which keeps local
    # development honest -- nothing is silently dropped or faked.
    email_backend: Literal["auto", "smtp", "console"] = "auto"

    # --- Email verification and password reset ----------------------------
    # When true, a new email/password account must confirm its address before it
    # can use Ember, and signup stops disclosing whether an address is already
    # registered.
    require_email_verification: bool = False
    email_verification_ttl_hours: int = 24
    password_reset_ttl_minutes: int = 60
    # Per-address budgets for the two token-issuing endpoints.
    password_reset_max_requests_per_ip: int = 10
    verification_max_requests_per_ip: int = 10

    # --- Pagination defaults ---------------------------------------------
    default_page_size: int = 50
    max_page_size: int = 100

    @field_validator("database_url")
    @classmethod
    def _require_postgres(cls, value: str) -> str:
        if not value.startswith(("postgresql://", "postgresql+psycopg://", "postgres://")):
            raise ValueError(
                "DATABASE_URL must be a PostgreSQL URL. SQLite is not supported."
            )
        # Normalise the driver so SQLAlchemy always uses psycopg 3.
        if value.startswith("postgres://"):
            value = "postgresql+psycopg://" + value[len("postgres://") :]
        elif value.startswith("postgresql://"):
            value = "postgresql+psycopg://" + value[len("postgresql://") :]
        return value

    @model_validator(mode="after")
    def _require_hardened_production(self) -> Settings:
        """Refuse to boot a production deployment with development defaults."""

        if self.environment != "production":
            return self
        problems: list[str] = []
        if self.session_secret == INSECURE_SESSION_SECRET:
            problems.append(
                "SESSION_SECRET is still the development placeholder. Generate one with "
                "`python -c \"import secrets; print(secrets.token_urlsafe(48))\"`."
            )
        if len(self.session_secret) < 32:
            problems.append("SESSION_SECRET must be at least 32 characters in production.")
        if not self.backend_url.startswith("https://"):
            problems.append("BACKEND_URL must use https:// in production.")
        if not self.frontend_url.startswith("https://"):
            problems.append("FRONTEND_URL must use https:// in production.")
        if self.require_email_verification and not self.smtp_configured:
            problems.append(
                "REQUIRE_EMAIL_VERIFICATION is on but SMTP is not configured, so nobody "
                "could ever confirm their address. Set SMTP_HOST and SMTP_FROM."
            )
        if problems:
            raise ValueError(
                "Unsafe production configuration:\n  - " + "\n  - ".join(problems)
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    @property
    def admin_github_username_set(self) -> frozenset[str]:
        return frozenset(
            part.strip().lower() for part in self.admin_github_usernames.split(",") if part.strip()
        )

    @property
    def admin_email_set(self) -> frozenset[str]:
        return frozenset(
            part.strip().lower() for part in self.admin_emails.split(",") if part.strip()
        )

    @property
    def github_scope_list(self) -> list[str]:
        return [s for s in self.github_oauth_scopes.split() if s]

    @property
    def github_oauth_configured(self) -> bool:
        return bool(self.github_client_id and self.github_client_secret)

    @property
    def cookie_secure(self) -> bool:
        return self.is_production or self.backend_url.startswith("https://")

    @property
    def smtp_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_from)

    @property
    def resolved_email_backend(self) -> Literal["smtp", "console"]:
        """Use SMTP when it is configured, otherwise log the message."""

        if self.email_backend == "auto":
            return "smtp" if self.smtp_configured else "console"
        return self.email_backend

    @property
    def email_delivery_enabled(self) -> bool:
        """True when mail actually leaves the machine."""

        return self.resolved_email_backend == "smtp"

    @property
    def cookie_samesite(self) -> Literal["lax", "strict", "none"]:
        # Same-origin deployment (Jinja/HTMX) -> "lax" is both safe and reliable.
        return "lax"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""

    return Settings()


settings = get_settings()
