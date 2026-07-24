"""Outbound email.

Two backends, chosen by configuration:

* ``smtp``    -- real delivery over SMTP (production)
* ``console`` -- the complete message, including the link, is written to the
  application log

The console backend exists so local development is *honest*: nothing is
silently dropped and no success is faked.  When SMTP is configured but the send
fails, the caller is told -- the flow never reports success for mail that did
not leave the machine.
"""

from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from app.core.config import settings
from app.core.errors import ExternalServiceError
from app.core.logging import get_logger

logger = get_logger("ember.mail")


@dataclass(slots=True)
class Email:
    to: str
    subject: str
    text_body: str
    html_body: str | None = None


@dataclass(slots=True)
class DeliveryResult:
    """What actually happened, so callers never have to guess."""

    delivered: bool
    backend: str


def _build_message(email: Email) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = email.subject
    message["From"] = formataddr(
        (settings.smtp_from_name, settings.smtp_from or "ember@localhost")
    )
    message["To"] = email.to
    message["Auto-Submitted"] = "auto-generated"
    message.set_content(email.text_body)
    if email.html_body:
        message.add_alternative(email.html_body, subtype="html")
    return message


def _send_smtp(email: Email) -> None:
    message = _build_message(email)
    try:
        if settings.smtp_port == 465:
            with smtplib.SMTP_SSL(
                settings.smtp_host,
                settings.smtp_port,
                timeout=settings.smtp_timeout_seconds,
                context=ssl.create_default_context(),
            ) as server:
                if settings.smtp_username:
                    server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(message)
            return
        with smtplib.SMTP(
            settings.smtp_host, settings.smtp_port, timeout=settings.smtp_timeout_seconds
        ) as server:
            if settings.smtp_use_tls:
                server.starttls(context=ssl.create_default_context())
            if settings.smtp_username:
                server.login(settings.smtp_username, settings.smtp_password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError) as exc:
        # The exception type only -- never the message body or credentials.
        logger.error("SMTP delivery failed: %s", type(exc).__name__)
        raise ExternalServiceError(
            "The email could not be sent. Please try again shortly.",
            code="EMAIL_DELIVERY_FAILED",
        ) from exc


def _send_console(email: Email) -> None:
    logger.info(
        "\n"
        "──────────────────── Ember email (console backend) ────────────────────\n"
        "To:      %s\n"
        "Subject: %s\n"
        "\n%s\n"
        "───────────────────────────────────────────────────────────────────────",
        email.to,
        email.subject,
        email.text_body,
    )


def send(email: Email) -> DeliveryResult:
    """Send an email. Raises :class:`ExternalServiceError` if SMTP refuses it."""

    backend = settings.resolved_email_backend
    if backend == "smtp":
        _send_smtp(email)
        return DeliveryResult(delivered=True, backend="smtp")
    _send_console(email)
    return DeliveryResult(delivered=False, backend="console")


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------

_SIGNATURE = "\n\n— Ember\nWhere cohort conversations turn into action."


def verification_email(*, to: str, display_name: str, link: str, ttl_hours: int) -> Email:
    text = (
        f"Hi {display_name},\n\n"
        "Confirm this address to finish setting up your Ember account:\n\n"
        f"{link}\n\n"
        f"The link expires in {ttl_hours} hours and can be used once.\n"
        "If you did not create an Ember account, you can ignore this email."
        f"{_SIGNATURE}"
    )
    html = f"""
      <p>Hi {display_name},</p>
      <p>Confirm this address to finish setting up your Ember account.</p>
      <p><a href="{link}"
            style="display:inline-block;background:#ee6008;color:#fff;padding:10px 18px;
                   border-radius:8px;text-decoration:none;font-weight:600">
        Confirm my email
      </a></p>
      <p style="color:#6f6c66;font-size:13px">
        The link expires in {ttl_hours} hours and can be used once.<br>
        If you did not create an Ember account, you can ignore this email.
      </p>
    """
    return Email(to=to, subject="Confirm your Ember email address", text_body=text, html_body=html)


def password_reset_email(*, to: str, display_name: str, link: str, ttl_minutes: int) -> Email:
    text = (
        f"Hi {display_name},\n\n"
        "Someone asked to reset the password for your Ember account. "
        "Use this link to choose a new one:\n\n"
        f"{link}\n\n"
        f"The link expires in {ttl_minutes} minutes and can be used once.\n"
        "If this was not you, no action is needed — your password has not changed."
        f"{_SIGNATURE}"
    )
    html = f"""
      <p>Hi {display_name},</p>
      <p>Someone asked to reset the password for your Ember account.</p>
      <p><a href="{link}"
            style="display:inline-block;background:#ee6008;color:#fff;padding:10px 18px;
                   border-radius:8px;text-decoration:none;font-weight:600">
        Choose a new password
      </a></p>
      <p style="color:#6f6c66;font-size:13px">
        The link expires in {ttl_minutes} minutes and can be used once.<br>
        If this was not you, no action is needed — your password has not changed.
      </p>
    """
    return Email(to=to, subject="Reset your Ember password", text_body=text, html_body=html)


def signup_attempt_email(*, to: str, display_name: str, sign_in_link: str) -> Email:
    """Sent when someone tries to sign up with an address that already exists.

    This is what lets the signup endpoint respond identically whether or not the
    address is registered, without leaving the real owner in the dark.
    """

    text = (
        f"Hi {display_name},\n\n"
        "Someone just tried to create an Ember account with this email address, "
        "but you already have one.\n\n"
        f"Sign in here: {sign_in_link}\n\n"
        "If you have forgotten your password, use the 'Forgot your password?' link "
        "on that page.\n"
        "If this was not you, no action is needed."
        f"{_SIGNATURE}"
    )
    return Email(to=to, subject="You already have an Ember account", text_body=text)
