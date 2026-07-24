"""Logging configuration.

Server logs never contain passwords, session tokens, OAuth tokens, or message
bodies.  :func:`scrub` is applied to any structured context we attach to a log
record.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

SENSITIVE_KEYS = frozenset(
    {
        "password",
        "password_hash",
        "new_password",
        "current_password",
        "token",
        "token_hash",
        "access_token",
        "refresh_token",
        "session_token",
        "client_secret",
        "session_secret",
        "authorization",
        "cookie",
        "set-cookie",
        "body",
        "message_body",
        "smtp_password",
    }
)

REDACTED = "[redacted]"


def scrub(data: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of ``data`` with sensitive values redacted."""

    cleaned: dict[str, Any] = {}
    for key, value in data.items():
        if key.lower() in SENSITIVE_KEYS:
            cleaned[key] = REDACTED
        elif isinstance(value, dict):
            cleaned[key] = scrub(value)
        else:
            cleaned[key] = value
    return cleaned


_CONFIGURED = False


def configure_logging(level: str = "INFO") -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s :: %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
