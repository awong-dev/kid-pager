"""Text helpers shared by the SMS/Voice paths (docs/PROTOCOL.md §3.1 body rules)."""

from __future__ import annotations

import re

_CONTROL_RE = re.compile("[\u0000-\u001f\u007f]")


def voice_link(e164: str) -> str:
    """The Voice web app addresses a thread by the peer's number."""
    return f"https://voice.google.com/u/0/messages?itemId=t.{e164}"


def pager_body(raw: str) -> str:
    """docs/PROTOCOL.md §3.1: control characters (U+0000-U+001F, U+007F) become spaces; strip."""
    return _CONTROL_RE.sub(" ", raw).strip()


def redact_phone(phone: str) -> str:
    """Last-4-digits only: the full number is PII nothing downstream needs."""
    return f"...{phone[-4:]}" if len(phone) >= 4 else "..."
