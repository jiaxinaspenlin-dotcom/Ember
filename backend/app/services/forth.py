"""Forth integration — a deliberately lightweight, link-only bridge.

Ember and **Forth** (the cohort project-management app at
``forth-bice.vercel.app``) keep entirely separate accounts and databases. This
module does **not** call any Forth API, share authentication, fetch task data,
or receive webhooks — Forth exposes none of those. It only validates and
recognises links to the one trusted Forth host, so members can hop between the
two tools without Ember ever inventing data it cannot actually see.

Validation always works on parsed URL components (never string containment), so
lookalike hosts, userinfo tricks, and unsafe schemes are rejected.
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from app.core.errors import ValidationError

# The single trusted Forth host. Matched exactly against the parsed hostname.
FORTH_HOST = "forth-bice.vercel.app"
FORTH_PROVIDER = "Forth"

_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
# Punctuation that commonly trails a URL in prose but is not part of it.
_TRAILING = ".,;:!?)]}>\"'"


def is_forth_url(value: str | None) -> bool:
    """True only for an ``https`` URL whose **exact** host is the Forth host.

    Because it parses the URL, lookalikes are all rejected:
    ``https://forth-bice.vercel.app.evil.com`` (host is ``…evil.com``),
    ``https://evil.com/?u=forth-bice.vercel.app`` (host is ``evil.com``),
    ``https://forth-bice.vercel.app@evil.com`` (host is ``evil.com``), and any
    non-https scheme.
    """

    if not value:
        return False
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    return parsed.scheme == "https" and (parsed.hostname or "").lower() == FORTH_HOST


def normalize_forth_url(value: str | None) -> str | None:
    """Validate a Forth URL and return it trimmed, or ``None`` for empty input.

    Raises :class:`ValidationError` for anything that is not an ``https`` URL on
    the exact Forth host.
    """

    cleaned = (value or "").strip()
    if not cleaned:
        return None
    try:
        parsed = urlparse(cleaned)
    except ValueError as exc:
        raise ValidationError(
            "That does not look like a valid URL.",
            code="FORTH_URL_INVALID",
            details={"field": "forth_url"},
        ) from exc
    if parsed.scheme != "https":
        raise ValidationError(
            "A Forth link must use https.",
            code="FORTH_URL_INVALID",
            details={"field": "forth_url"},
        )
    if (parsed.hostname or "").lower() != FORTH_HOST:
        raise ValidationError(
            f"A Forth link must point to {FORTH_HOST}.",
            code="FORTH_URL_INVALID",
            details={"field": "forth_url"},
        )
    return cleaned


def extract_forth_links(body: str | None) -> list[str]:
    """Distinct, order-preserving Forth URLs found in a message body.

    Used to render link-preview cards. Never fetches the URL or invents metadata.
    """

    found: list[str] = []
    for match in _URL_IN_TEXT.finditer(body or ""):
        candidate = match.group(0).rstrip(_TRAILING)
        if is_forth_url(candidate) and candidate not in found:
            found.append(candidate)
    return found


def display_path(url: str) -> str:
    """A compact, safe label for a Forth URL: its path (and query), host as root.

    Only ever called on URLs that already passed :func:`is_forth_url`.
    """

    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    return f"{FORTH_HOST}{path}"
