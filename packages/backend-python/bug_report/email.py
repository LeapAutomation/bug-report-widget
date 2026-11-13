"""Telling the reporter their report was closed, by email.

``create_blueprint(notifier=...)`` takes any ``notifier(notice) -> str`` that
returns a message id and raises on failure. This module ships the pieces a
Leap app needs: :class:`Notice` (what the blueprint hands over), :func:`render`
(subject + HTML, pure), and :class:`AcsNotifier` (Azure Communication Services
Email, behind the ``[email]`` extra, modelled on leap-daily-report's
``report_email.py``).

Nothing here imports Azure at import time, so ``import bug_report`` works
without the extra; an app that passes no notifier never emails.

Environment read by :meth:`AcsNotifier.from_env`:

``ACS_ENDPOINT``            managed identity (``DefaultAzureCredential``;
                            ``AZURE_CLIENT_ID`` picks a user-assigned identity)
``ACS_CONNECTION_STRING``   fallback when there is no endpoint
``BUG_NOTIFY_SENDER``       the From address, e.g. ``bugs@helloleap.ai``
``BUG_NOTIFY_DOMAINS``      comma list of domains it may write to; an address
                            outside it is refused (fail closed)
"""

from __future__ import annotations

import html as _html
import logging
import os
import re
from dataclasses import dataclass
from datetime import date

from .models import first_name, person_name

log = logging.getLogger(__name__)

FROM_NAME = "Leap bug reports"
SEND_TIMEOUT = 30  # seconds the poller may be waited on: bounded, and short enough for a click in the Bugs panel


@dataclass
class Notice:
    """One closed report, as the blueprint hands it to a notifier."""
    to_email: str
    to_name: str
    title: str
    note: str
    status: str            # "resolved" | "declined"
    bug_id: str
    filed_at: str = ""     # ISO timestamp
    page_url: str = ""     # the report's metaUrl
    app_url: str = ""
    closer_email: str = ""
    closer_name: str = ""


# ---------------------------------------------------------------------------
# Rendering (pure)
# ---------------------------------------------------------------------------

_URL_RE = re.compile(r"https?://[^\s<>\"']+")
_TRAILING = ".,;:!?)]}"


def _one_line(s: str) -> str:
    return " ".join((s or "").split())


def _subject(n: Notice) -> str:
    title = _one_line(n.title)
    if n.status == "resolved":
        return f"Fixed: {title}"
    if n.status == "declined":
        return f"Your bug report: {title} — not taken forward"
    raise ValueError(f"no email for status {n.status!r}")


def _note_html(note: str) -> str:
    """Escape the note, make http(s) URLs links, keep its line breaks."""
    out = []
    pos = 0
    for m in _URL_RE.finditer(note):
        url = m.group(0)
        tail = ""
        while url and url[-1] in _TRAILING:
            tail = url[-1] + tail
            url = url[:-1]
        out.append(_html.escape(note[pos:m.start()]))
        if url:
            u = _html.escape(url, quote=True)
            out.append(f'<a href="{u}">{u}</a>')
        out.append(_html.escape(tail))
        pos = m.end()
    out.append(_html.escape(note[pos:]))
    return "".join(out).replace("\r\n", "\n").replace("\n", "<br>\n")


def _pretty_date(iso: str) -> str:
    try:
        d = date.fromisoformat((iso or "")[:10])
    except ValueError:
        return ""
    return f"{d.day} {d:%B} {d.year}"


def render(notice: Notice) -> tuple[str, str]:
    """``(subject, html)`` for a closed report. Everything from the report is
    HTML-escaped; only http(s) URLs become links."""
    subject = _subject(notice)
    hi = first_name(notice.to_name, notice.to_email)
    closer = person_name(notice.closer_name, notice.closer_email)
    when = _pretty_date(notice.filed_at)
    link = next((u for u in (notice.page_url, notice.app_url)
                 if (u or "").startswith(("http://", "https://"))), "")

    parts = [f"<p>Hi {_html.escape(hi)},</p>" if hi else "<p>Hello,</p>"]
    parts.append(f"<p>{_note_html(notice.note.strip())}</p>")
    parts.append(f"<p>— {_html.escape(closer or FROM_NAME)}</p>")

    said = "You reported this"
    if when:
        said += f" on {when}"
    if link:
        u = _html.escape(link, quote=True)
        said += f' from <a href="{u}">{u}</a>'
    parts.append(f'<p style="color:#666;font-size:13px">{said}.<br>'
                 f"Report {_html.escape(notice.bug_id)}</p>")
    body = "\n".join(parts)
    return subject, f'<div style="font-family:sans-serif;font-size:15px">\n{body}\n</div>'


# ---------------------------------------------------------------------------
# Sending (Azure Communication Services Email)
# ---------------------------------------------------------------------------

def _domain(address: str) -> str:
    return address.rsplit("@", 1)[-1].strip().lower() if "@" in address else ""


class AcsNotifier:
    """``notifier(notice) -> message id`` over ACS Email.

    Fails closed: no sender, or an address outside ``domains``, raises before
    anything is sent. ``retry_total=0`` because the SDK's own retry re-sends a
    throttled request immediately, and each attempt counts against the quota
    that refused it (the daily report's lesson, 2026-09-15). The wait on the
    poller is bounded by ``timeout``.
    """

    def __init__(self, *, sender: str, domains, endpoint: str = "",
                 connection_string: str = "", client_id: str = "",
                 client=None, timeout: float = SEND_TIMEOUT) -> None:
        self.sender = (sender or "").strip()
        if isinstance(domains, str):
            domains = domains.split(",")
        self.domains = tuple(d.strip().lstrip("@").lower() for d in (domains or ()) if d.strip())
        self.endpoint = (endpoint or "").strip()
        self.connection_string = (connection_string or "").strip()
        self.client_id = (client_id or "").strip()
        self.timeout = timeout
        self._client = client

    @classmethod
    def from_env(cls, env=None):
        """The notifier the environment describes, or ``None`` when it does not
        (no credential, no sender, or no allowed domains), so an app can write
        ``notifier=AcsNotifier.from_env()`` and simply not email when unset."""
        env = os.environ if env is None else env
        endpoint = env.get("ACS_ENDPOINT", "").strip()
        conn = env.get("ACS_CONNECTION_STRING", "").strip()
        sender = env.get("BUG_NOTIFY_SENDER", "").strip()
        domains = env.get("BUG_NOTIFY_DOMAINS", "").strip()
        if not (endpoint or conn) or not sender:
            return None
        if not domains:
            log.warning("bug notify: BUG_NOTIFY_DOMAINS not set; emailing reporters is off (fail closed)")
            return None
        return cls(sender=sender, domains=domains, endpoint=endpoint,
                   connection_string=conn, client_id=env.get("AZURE_CLIENT_ID", ""))

    def _get_client(self):
        if self._client is None:
            from azure.communication.email import EmailClient  # lazy: the [email] extra
            if self.endpoint:
                from azure.identity import DefaultAzureCredential
                kw = {"managed_identity_client_id": self.client_id} if self.client_id else {}
                self._client = EmailClient(self.endpoint, DefaultAzureCredential(**kw), retry_total=0)
            elif self.connection_string:
                self._client = EmailClient.from_connection_string(self.connection_string, retry_total=0)
            else:
                raise RuntimeError("neither ACS_ENDPOINT nor ACS_CONNECTION_STRING set (fail closed)")
        return self._client

    def __call__(self, notice: Notice) -> str:
        if not self.sender:
            raise RuntimeError("BUG_NOTIFY_SENDER not set (fail closed)")
        to = (notice.to_email or "").strip()
        if not to:
            raise RuntimeError("no address to send to")
        if not self.domains:
            raise RuntimeError("BUG_NOTIFY_DOMAINS not set (fail closed)")
        if _domain(to) not in self.domains:
            raise RuntimeError(f"{_domain(to) or to} is not an allowed domain; not sent")
        subject, body = render(notice)
        recipient = {"address": to}
        if notice.to_name:
            recipient["displayName"] = notice.to_name
        message = {
            "senderAddress": self.sender,
            "content": {"subject": subject, "html": body},
            "recipients": {"to": [recipient]},
        }
        if notice.closer_email:
            reply = {"address": notice.closer_email}
            name = person_name(notice.closer_name, notice.closer_email)
            if name:
                reply["displayName"] = name
            message["replyTo"] = [reply]
        poller = self._get_client().begin_send(message)
        result = poller.result(timeout=self.timeout)
        # A wait that runs out returns whatever the poller last saw (None, or
        # a "Running" status), not an error: never audit that as "emailed".
        if not poller.done():
            raise RuntimeError(f"ACS had not confirmed the send after {self.timeout:g} s; it may still arrive")
        get = result.get if isinstance(result, dict) else (lambda k, d=None: getattr(result, k, d))
        status = getattr(get("status"), "value", get("status"))
        if status is not None and str(status).lower() != "succeeded":
            raise RuntimeError(f"ACS reported {status}")
        return str(get("id") or "")
