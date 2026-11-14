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
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields as dc_fields
from pathlib import Path
from typing import Iterable, Protocol

from . import contract

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
    # Contract v3 (backends 3.0.0). A record written before has none of these
    # and loads with the defaults: null, and no votes.
    app: str | None = None        # stamped by the proxy from the caller's identity
    kind: str | None = None       # stored; derived from `tags` when None
    area: str | None = None       # triage
    priority: str | None = None   # triage: now | next | later
    owner: str | None = None      # triage: an email
    parent_id: str | None = None  # triage: a report id (an epic holds its children)
    duplicate_of: str | None = None  # triage: a report id

    @classmethod
    def from_dict(cls, data: dict) -> "BugRecord":
        """A record from its stored JSON. Keys this version does not know are
        dropped (a newer writer's fields must not make the report unreadable);
        keys it knows but the data lacks take their defaults."""
        known = {f.name for f in dc_fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def effective_kind(self) -> str:
        return contract.effective_kind(self.kind, self.tags)

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
            "kind": self.effective_kind(),
            "app": self.app,
            "product": contract.product_for_app(self.app),
            "area": self.area,
            "priority": self.priority,
            "owner": self.owner,
            "parentId": self.parent_id,
            "duplicateOf": self.duplicate_of,
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


def matches(rec: BugRecord, *, status=None, since_iso="", app=None, kind=None,
            area=None, priority=None) -> bool:
    """The list filters, in one place so every store agrees on them."""
    if rec.added_at < (since_iso or ""):
        return False
    if status and rec.status != status:
        return False
    if app and rec.app != app:
        return False
    if kind and rec.effective_kind() != kind:
        return False
    if area and rec.area != area:
        return False
    if priority and rec.priority != priority:
        return False
    return True


def vote_time() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def normal_voter(voter: str) -> str:
    """One vote per PERSON: addresses compare case-insensitively."""
    return (voter or "").strip().lower()


# Fields `update_fields` may set: the record's snake_case attributes.
TRIAGE_ATTRS = tuple(contract.TRIAGE_WIRE_TO_ATTR.values())


# ---------------------------------------------------------------------------
# Store protocol
# ---------------------------------------------------------------------------

class Store(Protocol):
    """Abstract storage. Implementations must be thread-safe enough for
    a single Flask worker; cross-process concurrency is each impl's call."""

    def put_bug(self, bug: BugRecord, screenshot_png: bytes | None) -> None: ...
    def get_bug(self, bug_id: str) -> BugRecord: ...
    def list_bugs(self, *, status: str | None, since_iso: str, app: str | None = None,
                  kind: str | None = None, area: str | None = None,
                  priority: str | None = None) -> Iterable[BugRecord]: ...
    def update_status(self, bug_id: str, new_status: str) -> BugRecord: ...
    # v3 (backends 3.0.0)
    def update_fields(self, bug_id: str, changes: dict) -> BugRecord:
        """Set triage attributes (``TRIAGE_ATTRS``: area, priority, owner,
        parent_id, duplicate_of); a None value clears one."""
        ...
    def update_bug(self, bug_id: str, *, status: str | None = None,
                   changes: dict | None = None) -> BugRecord:
        """Set the status and/or triage attributes ATOMICALLY: read, change and
        write as one step, so two concurrent PATCHes of different fields both
        land (no lost update). What the PATCH route calls."""
        ...
    def add_vote(self, bug_id: str, voter: str, at: str | None = None) -> bool:
        """Record ``voter``'s vote (at ``at``, default now); False when they had already voted."""
        ...
    def remove_vote(self, bug_id: str, voter: str) -> bool: ...
    def vote_counts(self, bug_ids: list[str]) -> dict[str, int]: ...
    def list_votes(self, bug_id: str) -> list[dict]:
        """``[{"voter", "at"}]``, oldest first."""
        ...
    def append_audit(self, bug_id: str, entry: AuditEntry) -> None: ...
    def list_audit(self, bug_id: str) -> list[AuditEntry]: ...
    def get_screenshot(self, bug_id: str) -> bytes: ...


def apply_changes(rec: BugRecord, changes: dict, status: str | None = None) -> None:
    for k, v in changes.items():
        if k not in TRIAGE_ATTRS:
            raise StoreError(f"not a triage field: {k}")
        setattr(rec, k, v)
    if status is not None:
        rec.status = status


# ---------------------------------------------------------------------------
# InMemoryStore — for tests
# ---------------------------------------------------------------------------

class InMemoryStore:
    """Dict-backed store. Lost on process exit. Useful for tests + dev."""

    def __init__(self) -> None:
        self._bugs: dict[str, BugRecord] = {}
        self._audits: dict[str, list[AuditEntry]] = {}
        self._screenshots: dict[str, bytes] = {}
        self._votes: dict[str, dict[str, str]] = {}

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

    def list_bugs(self, *, status, since_iso, app=None, kind=None, area=None, priority=None):
        for b in list(self._bugs.values()):
            if matches(b, status=status, since_iso=since_iso, app=app, kind=kind,
                       area=area, priority=priority):
                yield b

    def update_status(self, bug_id, new_status):
        b = self.get_bug(bug_id)
        b.status = new_status
        return b

    def update_fields(self, bug_id, changes):
        b = self.get_bug(bug_id)
        apply_changes(b, changes)
        return b

    def update_bug(self, bug_id, *, status=None, changes=None):
        b = self.get_bug(bug_id)
        apply_changes(b, changes or {}, status)
        return b

    def add_vote(self, bug_id, voter, at=None):
        self.get_bug(bug_id)
        v = normal_voter(voter)
        votes = self._votes.setdefault(bug_id, {})
        if v in votes:
            return False
        votes[v] = at or vote_time()
        return True

    def remove_vote(self, bug_id, voter):
        self.get_bug(bug_id)
        return self._votes.get(bug_id, {}).pop(normal_voter(voter), None) is not None

    def vote_counts(self, bug_ids):
        return {i: len(self._votes.get(i, {})) for i in bug_ids}

    def list_votes(self, bug_id):
        return [{"voter": v, "at": at} for v, at in self._votes.get(bug_id, {}).items()]

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
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock(self, bug_id: str) -> threading.Lock:
        """One lock per report: a read-modify-write of its record (status, triage
        fields, votes) must not interleave with another, or the later write
        puts back the older value of whatever the other one changed. (One
        process; across processes each write is still atomic, as before.)"""
        with self._locks_guard:
            return self._locks.setdefault(bug_id, threading.Lock())

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

    def _votes_path(self, bug_id: str) -> Path:
        return self.root / f"{bug_id}.votes.jsonl"

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
        return BugRecord.from_dict(data)

    def list_bugs(self, *, status, since_iso, app=None, kind=None, area=None, priority=None):
        for p in self.root.glob("bug-*.json"):
            if p.name.endswith(".audit.jsonl"):
                continue
            try:
                data = _read_json(p)
            except (OSError, json.JSONDecodeError):
                continue
            rec = BugRecord.from_dict(data)
            if matches(rec, status=status, since_iso=since_iso, app=app, kind=kind,
                       area=area, priority=priority):
                yield rec

    def update_status(self, bug_id, new_status):
        return self.update_bug(bug_id, status=new_status)

    def update_fields(self, bug_id, changes):
        return self.update_bug(bug_id, changes=changes)

    def update_bug(self, bug_id, *, status=None, changes=None):
        with self._lock(bug_id):
            rec = self.get_bug(bug_id)
            apply_changes(rec, changes or {}, status)
            self.put_bug(rec, None)
            return rec

    def _read_votes(self, bug_id) -> dict[str, str]:
        p = self._votes_path(bug_id)
        out: dict[str, str] = {}
        if not p.exists():
            return out
        for ln in p.read_text().splitlines():
            try:
                d = json.loads(ln)
                out[d["voter"]] = d.get("at", "")
            except (json.JSONDecodeError, KeyError, TypeError):
                continue
        return out

    def _write_votes(self, bug_id, votes: dict[str, str]) -> None:
        body = "".join(json.dumps({"voter": v, "at": at}) + "\n" for v, at in votes.items())
        _atomic_write(self._votes_path(bug_id), body.encode())

    def add_vote(self, bug_id, voter, at=None):
        self.get_bug(bug_id)
        v = normal_voter(voter)
        with self._lock(bug_id):
            votes = self._read_votes(bug_id)
            if v in votes:
                return False
            votes[v] = at or vote_time()
            self._write_votes(bug_id, votes)
            return True

    def remove_vote(self, bug_id, voter):
        self.get_bug(bug_id)
        with self._lock(bug_id):
            votes = self._read_votes(bug_id)
            if votes.pop(normal_voter(voter), None) is None:
                return False
            self._write_votes(bug_id, votes)
            return True

    def vote_counts(self, bug_ids):
        return {i: len(self._read_votes(i)) for i in bug_ids}

    def list_votes(self, bug_id):
        return [{"voter": v, "at": at} for v, at in self._read_votes(bug_id).items()]

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
