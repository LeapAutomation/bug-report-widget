"""Model-layer constants + ID helpers. Pure Python; no Flask, no I/O."""

from __future__ import annotations

import re
import time
import uuid

BUG_DETAILS_MAX = 10 * 1024          # 10 KB
BUG_SCREENSHOT_MAX = 5 * 1024 * 1024  # 5 MB decoded

BUG_STATUSES = ("open", "triaged", "resolved", "declined")

# The widget serialises PNG when it fits and falls back down a JPEG ladder when
# the capture would otherwise blow the cap above — so "the screenshot" is not
# always a PNG, and on an image-heavy page it usually is not. Serving every one
# of them as image/png mislabels the common case; a consumer that sends
# `X-Content-Type-Options: nosniff` is then relying on browsers being lenient
# about image types to render its own triage queue.
#
# Sniffed rather than stored, deliberately: the bytes are the only thing every
# Store implementation is guaranteed to have kept, so this also fixes the
# screenshots already sitting in existing stores. Unrecognised bytes keep the
# historical image/png, which is what every deployment before this returned.
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)


def screenshot_content_type(data: bytes) -> str:
    """The media type of a stored screenshot, from its magic bytes."""
    if not data:
        return "image/png"
    for magic, mime in _MAGIC:
        if data.startswith(magic):
            return mime
    # WebP is RIFF-framed: "RIFF" <4-byte size> "WEBP".
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/png"

_BUG_ID_RE = re.compile(r"^bug-\d{8}-\d{6}-[a-f0-9]{6}$")


def new_bug_id() -> str:
    """`bug-YYYYMMDD-HHMMSS-XXXXXX`. Sortable by timestamp; the suffix
    disambiguates within the same second."""
    return f"bug-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}-{uuid.uuid4().hex[:6]}"


def valid_bug_id(bug_id: str) -> bool:
    return bool(_BUG_ID_RE.match(bug_id or ""))


def person_name(name: str | None, email: str | None) -> str:
    """The display name of a person: the stored ``name``, else one derived from
    the address's local part (``chris.hamblin@x`` -> ``Chris Hamblin``, ``ken@x``
    -> ``Ken``), else "". The same rule the UI uses; the two must agree."""
    name = (name or "").strip()
    if name:
        return name
    local = (email or "").strip().split("@", 1)[0]
    parts = [p for p in re.split(r"[._-]+", local) if p]
    return " ".join(p[:1].upper() + p[1:] for p in parts)


def first_name(name: str | None, email: str | None = "") -> str:
    """First word of :func:`person_name`."""
    full = person_name(name, email)
    return full.split(None, 1)[0] if full else ""
