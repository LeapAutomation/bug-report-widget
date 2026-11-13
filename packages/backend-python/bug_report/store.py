"""Storage interface + reference implementations.

The blueprint depends only on ``Store``. Apps that want a different backend
(SQLite, Postgres, S3, …) implement the protocol and pass an instance in.

Two implementations ship out of the box:
- ``InMemoryStore``  — for tests and dev. State is process-local.
- ``FilesystemStore`` — one JSON file per bug + one screenshot PNG.
                        Adequate for single-host apps (this is what the
                        claude-tmux-dashboard uses in production).
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Protocol

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class StoreError(Exception):
    """Base for store errors."""


class NotFound(StoreError):
    """Bug or screenshot does not exist."""


# ---------------------------------------------------------------------------
# Record + audit shapes
# ---------------------------------------------------------------------------

@dataclass
class BugRecord:
    id: str
    title: str
    details: str
    tags: list[str]
    status: str
    added_by: str
    actor_email: str
    added_at: str
    meta_url: str = ""
    meta_user_agent: str = ""
    meta_build_sha: str = ""
    screenshot: bool = False
    transcript: list[dict] | None = None
    # Display name of whoever filed it ("" when unknown). Added in 2.2.0; a
    # record on disk from before has no such key and loads with the default.
    actor_name: str = ""

    def to_summary(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "tags": list(self.tags),
            "status": self.status,
            "addedBy": self.added_by,
            "actorEmail": self.actor_email,
            "actorName": self.actor_name,
            "addedAt": self.added_at,
            "screenshot": bool(self.screenshot),
        }

    def to_full(self) -> dict:
        d = self.to_summary()
        d.update({
            "details": self.details,
            "metaUrl": self.meta_url,
            "metaUserAgent": self.meta_user_agent,
            "metaBuildSha": self.meta_build_sha,
            "transcript": self.transcript,
        })
        return d


@dataclass
class AuditEntry:
    changed_at: str
    to_status: str
    from_status: str | None = None
    changed_by: str = ""
    note: str = ""
    changed_by_name: str = ""  # display name of changed_by; added in 2.2.0

    def to_dict(self) -> dict:
        return {
            "changedAt": self.changed_at,
            "changedBy": self.changed_by,
            "changedByName": self.changed_by_name,
            "fromStatus": self.from_status,
            "toStatus": self.to_status,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------

class Store(Protocol):
    """Abstract storage. Implementations must be thread-safe enough for
    a single Flask worker; cross-process concurrency is each impl's call."""

    def put_bug(self, bug: BugRecord, screenshot_png: bytes | None) -> None: ...
    def get_bug(self, bug_id: str) -> BugRecord: ...
    def list_bugs(self, *, status: str | None, since_iso: str) -> Iterable[BugRecord]: ...
    def update_status(self, bug_id: str, new_status: str) -> BugRecord: ...
    def append_audit(self, bug_id: str, entry: AuditEntry) -> None: ...
    def list_audit(self, bug_id: str) -> list[AuditEntry]: ...
    def get_screenshot(self, bug_id: str) -> bytes: ...


# ---------------------------------------------------------------------------
# InMemoryStore — for tests
# ---------------------------------------------------------------------------

class InMemoryStore:
    """Dict-backed store. Lost on process exit. Useful for tests + dev."""

    def __init__(self) -> None:
        self._bugs: dict[str, BugRecord] = {}
        self._audits: dict[str, list[AuditEntry]] = {}
        self._screenshots: dict[str, bytes] = {}

    def put_bug(self, bug: BugRecord, screenshot_png: bytes | None) -> None:
        self._bugs[bug.id] = bug
        if screenshot_png is not None:
            self._screenshots[bug.id] = screenshot_png
            bug.screenshot = True

    def get_bug(self, bug_id: str) -> BugRecord:
        try:
            return self._bugs[bug_id]
        except KeyError as e:
            raise NotFound(bug_id) from e

    def list_bugs(self, *, status, since_iso):
        for b in self._bugs.values():
            if b.added_at < since_iso:
                continue
            if status and b.status != status:
                continue
            yield b

    def update_status(self, bug_id, new_status):
        b = self.get_bug(bug_id)
        b.status = new_status
        return b

    def append_audit(self, bug_id, entry):
        self._audits.setdefault(bug_id, []).append(entry)

    def list_audit(self, bug_id):
        return list(self._audits.get(bug_id, []))

    def get_screenshot(self, bug_id):
        if bug_id not in self._screenshots:
            raise NotFound(bug_id)
        return self._screenshots[bug_id]

# ---------------------------------------------------------------------------
# Atomic writes
# ---------------------------------------------------------------------------

READ_ATTEMPTS = 5          # a report that will not parse is re-read this many times…
READ_PAUSE = 0.02          # …this many seconds apart, before it is called unreadable
STALE_TMP_SECONDS = 600    # a temp file older than this is a crashed write's leftover


def _atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` so a reader sees the old file or the new one.

    ``Path.write_text`` truncates, then writes: a reader in between sees an empty
    or partial file, and a crash leaves one behind. Instead: a temp file beside
    the target (same directory, so the rename stays on one filesystem), fsynced,
    then ``os.replace``d over it. The temp is dot-prefixed and ends ``.tmp``, so
    the ``bug-*.json`` listing never matches it.

    Azure Files (SMB) supports rename over an existing file but can refuse it
    while another handle holds the target open; a refused replace falls back to
    the in-place write this store always did (readers retry, see ``_read_json``).
    """
    # os.open with 0o666 gets the mode a plain write would (0666 & ~umask, applied by
    # the kernel); tempfile.mkstemp would make it 0600.
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex[:12]}.tmp"
    fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o666)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        # the temp write failed: the target is untouched, so do NOT fall back to
        # an in-place write (it would truncate the good copy)
        _unlink_quietly(tmp)
        raise
    try:
        os.replace(tmp, path)
    except OSError as e:
        _unlink_quietly(tmp)
        log.warning("bug store: os.replace refused for %s (%s); writing in place", path.name, e)
        path.write_bytes(data)


def _unlink_quietly(path) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _read_json(path: Path):
    """Parse a JSON file, re-reading briefly if it will not parse (a writer that
    could not rename, or a report written by an older version of this store)."""
    for attempt in range(READ_ATTEMPTS):
        try:
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            if attempt == READ_ATTEMPTS - 1:
                raise
            time.sleep(READ_PAUSE)


# ---------------------------------------------------------------------------
# FilesystemStore — production-grade for single-host apps
# ---------------------------------------------------------------------------

class FilesystemStore:
    """One JSON file per bug, audit lines in JSONL, PNG bytes alongside.

    Layout under ``root``::

        bugs/
          bug-20260516-181523-a3b91f2.json
          bug-20260516-181523-a3b91f2.audit.jsonl
          bug-20260516-181523-a3b91f2.png        (optional)
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._sweep_stale_temps()

    def _sweep_stale_temps(self) -> None:
        """A crash between creating a temp file and the replace leaves it behind."""
        cutoff = time.time() - STALE_TMP_SECONDS
        for p in self.root.glob(".*.tmp"):
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass

    def _rec_path(self, bug_id: str) -> Path:
        return self.root / f"{bug_id}.json"

    def _audit_path(self, bug_id: str) -> Path:
        return self.root / f"{bug_id}.audit.jsonl"

    def _shot_path(self, bug_id: str) -> Path:
        return self.root / f"{bug_id}.png"

    def put_bug(self, bug, screenshot_png):
        if screenshot_png is not None:
            _atomic_write(self._shot_path(bug.id), screenshot_png)
            bug.screenshot = True
        # Convert dataclass → dict carefully (we want camelCase on-disk shape).
        payload = asdict(bug)
        _atomic_write(self._rec_path(bug.id), json.dumps(payload, indent=2).encode())

    def get_bug(self, bug_id):
        p = self._rec_path(bug_id)
        if not p.exists():
            raise NotFound(bug_id)
        try:
            data = _read_json(p)
        except (OSError, json.JSONDecodeError) as e:
            raise StoreError(f"could not read {bug_id}") from e
        return BugRecord(**data)

    def list_bugs(self, *, status, since_iso):
        for p in self.root.glob("bug-*.json"):
            if p.name.endswith(".audit.jsonl"):
                continue
            try:
                data = _read_json(p)
            except (OSError, json.JSONDecodeError):
                continue
            rec = BugRecord(**data)
            if rec.added_at < since_iso:
                continue
            if status and rec.status != status:
                continue
            yield rec

    def update_status(self, bug_id, new_status):
        rec = self.get_bug(bug_id)
        rec.status = new_status
        self.put_bug(rec, None)
        return rec

    def append_audit(self, bug_id, entry):
        # An append, not a rewrite: it never truncates, and one short line is a
        # single O_APPEND write, so a reader sees whole lines (list_audit skips a torn one).
        with open(self._audit_path(bug_id), "a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def list_audit(self, bug_id):
        p = self._audit_path(bug_id)
        if not p.exists():
            return []
        out = []
        for ln in p.read_text().splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                d = json.loads(ln)
            except json.JSONDecodeError:
                continue
            # Accept both camelCase (current wire format) and snake_case
            # (legacy on-disk format written by the claude-tmux-dashboard
            # before it adopted this package). Lenient read; strict write.
            out.append(AuditEntry(
                changed_at=d.get("changedAt") or d.get("changed_at", ""),
                to_status=d.get("toStatus") or d.get("to_status", ""),
                from_status=d.get("fromStatus") if "fromStatus" in d else d.get("from_status"),
                changed_by=d.get("changedBy") or d.get("changed_by", ""),
                note=d.get("note", ""),
                changed_by_name=d.get("changedByName") or d.get("changed_by_name", ""),
            ))
        return out

    def get_screenshot(self, bug_id):
        p = self._shot_path(bug_id)
        if not p.exists():
            raise NotFound(bug_id)
        return p.read_bytes()
